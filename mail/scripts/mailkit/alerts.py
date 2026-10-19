"""The texts of alerts, and the queue of chat messages that wait for the chat.

An alert says which mailbox, who sent the mail, its subject, why it matters, the amount and the
deadline when the reader found them, and a warning when the reader took it for phishing. All of it
comes from the verdict: the sender and subject from the mail's headers, the rest from the reader's
fixed fields, never the body. More than COMBINE_OVER urgent mails at once (a catch-up after the
server was down) make one message with a line per mail, not a storm of messages.

`state/alerts.json` holds the messages that wait for the chat (`pending`, in the order they came)
and a record of the ones that went out later (`sent`: their mail and times, not their text, kept
for KEEP). The watcher queues a message that the chat did not take and sends the queue when the
chat is back; the daily report counts the alerts that went out late, `alerts-status` what still
waits. Reads and writes take the state lock, which is never held while a message is sent.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from . import clock
from .chat import ChatError
from .context import Ctx
from .paths import locked, read_json, write_json
from .query import clean
from .reader import Verdict
from .strings import t

COMBINE_OVER = 3
KEEP = timedelta(days=62)
FIELD_LIMIT = 300  # a field of a verdict as one line of an alert
ALERTS = "alerts.json"  # in the state directory


def _field(text: str, language: str) -> str:
    """A field of a verdict as one clean line (a stored verdict could hold anything), or the word
    for unknown when it is empty."""
    return clean(text, FIELD_LIMIT) or t("unknown", language)


def alert_text(v: Verdict, language: str) -> str:
    """The alert of one urgent mail, a line per fact."""
    lines = [
        t("alert_head", language, mailbox=v.mailbox),
        t("show_from", language, value=_field(v.sender, language)),
        t("show_subject", language, value=_field(v.subject, language)),
    ]
    if v.reason:
        lines.append(t("alert_reason", language, reason=clean(v.reason, FIELD_LIMIT)))
    if v.amount:
        lines.append(t("alert_amount", language, amount=clean(v.amount, FIELD_LIMIT)))
    if v.deadline:
        lines.append(t("alert_deadline", language, deadline=clean(v.deadline, FIELD_LIMIT)))
    if v.phishing:
        lines.append(t("alert_phishing", language))
    return "\n".join(lines)


def _line(v: Verdict, language: str) -> str:
    parts = [f"[{v.mailbox}] {_field(v.sender, language)}: {_field(v.subject, language)}"]
    parts += [clean(text, FIELD_LIMIT) for text in (v.reason, v.amount, v.deadline) if text]
    if v.phishing:
        parts.append(f"⚠ {t('alert_phishing_short', language)}")
    return "- " + " · ".join(parts)


def combined_text(vs: list[Verdict], language: str) -> str:
    """One alert for many urgent mails: a line per mail, in their order."""
    return "\n".join([t("alert_many", language, count=len(vs)), *(_line(v, language) for v in vs)])


# --- the queue ---------------------------------------------------------------------------------


def _time(text: object) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        found = datetime.fromisoformat(text)
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def _iso(at: datetime) -> str:
    return (at if at.tzinfo else at.replace(tzinfo=UTC)).isoformat()


def _keys(entry: dict) -> bool:
    keys = entry.get("keys")
    return isinstance(keys, list) and all(isinstance(key, str) for key in keys)


class AlertQueue:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.path = ctx.state / ALERTS

    def _load(self) -> dict:
        """The queue as the file has it, without what is not an entry of it. Under the lock."""
        data = read_json(self.path, {})
        data = data if isinstance(data, dict) else {}
        pending, sent = data.get("pending"), data.get("sent")
        return {
            "pending": [
                e for e in (pending if isinstance(pending, list) else [])
                if isinstance(e, dict) and isinstance(e.get("id"), str)
                and isinstance(e.get("text"), str) and _keys(e) and _time(e.get("queued"))],
            "sent": [
                e for e in (sent if isinstance(sent, list) else [])
                if isinstance(e, dict) and _keys(e) and _time(e.get("queued"))
                and _time(e.get("sent"))],
        }

    def _write(self, data: dict) -> None:
        cutoff = clock.now() - KEEP
        data["sent"] = [e for e in data["sent"] if _time(e["sent"]) >= cutoff]
        write_json(self.path, data)

    def add(self, text: str, keys: Iterable[str], at: datetime) -> None:
        """Queue `text`, the alert of the mails `keys` (none for a notice), as of `at`."""
        entry = {"id": uuid.uuid4().hex, "text": text, "keys": list(keys), "queued": _iso(at)}
        with locked(self.ctx.state):
            data = self._load()
            data["pending"].append(entry)
            self._write(data)

    def pending(self) -> list[dict]:
        """The messages that wait, oldest first: `id`, `text`, `keys` and `queued`."""
        with locked(self.ctx.state):
            return self._load()["pending"]

    def drain(self, send: Callable[[str], None], at: datetime) -> int:
        """Send the waiting messages with `send`, oldest first, until one fails with a ChatError
        (it and the ones after it wait on); how many went out. Each is taken off the queue once
        it went out, so a failure or a crash halfway loses none."""
        count = 0
        while True:
            waiting = self.pending()
            if not waiting:
                return count
            first = waiting[0]
            try:
                send(first["text"])
            except ChatError:
                return count
            with locked(self.ctx.state):
                data = self._load()
                data["pending"] = [e for e in data["pending"] if e["id"] != first["id"]]
                data["sent"].append({
                    "id": first["id"], "keys": first["keys"], "queued": first["queued"],
                    "sent": _iso(at)})
                self._write(data)
            count += 1

    def late(self, since: datetime, until: datetime) -> list[dict]:
        """The alerts that went out later than they were queued, in [since, until) by the time
        they went out: `keys`, `queued` and `sent` (ISO 8601). Notices, which name no mail, are
        not alerts and are left out."""
        start, end = (at if at.tzinfo else at.replace(tzinfo=UTC) for at in (since, until))
        with locked(self.ctx.state):
            sent = self._load()["sent"]
        return [
            {"keys": e["keys"], "queued": e["queued"], "sent": e["sent"]} for e in sent
            if e["keys"] and start <= _time(e["sent"]) < end and _time(e["sent"]) > _time(
                e["queued"])]

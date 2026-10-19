"""The reader's verdicts, kept so that a mail is sorted once and alerted once.

`state/verdicts.json` maps a verdict key (`marks.verdict_key`) to the verdict, the time it was
stored and the time its alert went out. The watcher and the daily report share it: what one sorted,
the other reuses. Every write takes the state lock (`paths.locked`) and replaces the file whole, so
a reader sees the old file or the new one and two writers, in threads or processes, never lose
each other's entries. The lock is not reentrant: do not call a writing method while holding it.

An entry is dropped on a write once its mail is older than KEEP: by the mail's date, which counts
as no later than the time it was stored (a Date header far ahead cannot keep an entry for ever),
or by that time when the mail has no date. An entry whose alert went out within KEEP stays, so a
mail with a back-dated Date header is never alerted twice.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from . import clock
from .context import Ctx
from .paths import locked, read_json, write_json
from .reader import Verdict

KEEP = timedelta(days=62)
VERDICTS = "verdicts.json"  # in the state directory


def _time(text: object) -> datetime | None:
    """An ISO 8601 time as an aware datetime (no offset: UTC), else None."""
    if not isinstance(text, str) or not text:
        return None
    try:
        found = datetime.fromisoformat(text)
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def _verdict(entry: object) -> Verdict | None:
    fields = entry.get("verdict") if isinstance(entry, dict) else None
    if not isinstance(fields, dict):
        return None
    try:
        return Verdict(**fields)
    except TypeError:  # not what `put` wrote
        return None


def _kept(entry: object, cutoff: datetime) -> bool:
    if not isinstance(entry, dict):
        return False
    alerted = _time(entry.get("alerted"))
    if alerted is not None and alerted >= cutoff:
        return True
    stored = _time(entry.get("stored"))
    found = _verdict(entry)
    dated = _time(found.date) if found else None
    if dated is not None and stored is not None:
        dated = min(dated, stored)
    since = dated or stored
    return since is not None and since >= cutoff


class VerdictStore:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.path = ctx.state / VERDICTS

    def _entries(self) -> dict:
        data = read_json(self.path, {})
        return data if isinstance(data, dict) else {}

    def _write(self, entries: dict) -> None:
        """Write the entries that are kept (see the module's docstring). Under the lock."""
        cutoff = clock.now() - KEEP
        write_json(self.path, {key: e for key, e in entries.items() if _kept(e, cutoff)})

    def get(self, key: str) -> Verdict | None:
        return self.get_many([key]).get(key)

    def get_many(self, keys: Iterable[str]) -> dict[str, Verdict]:
        """The stored verdicts of these keys, read at once."""
        entries = self._entries()
        found = {}
        for key in keys:
            verdict = _verdict(entries.get(key))
            if verdict is not None and verdict.key == key:
                found[key] = verdict
        return found

    def put(self, v: Verdict) -> None:
        self.put_many([v])

    def put_many(self, verdicts: Iterable[Verdict]) -> None:
        """Store verdicts in one write. A verdict stored again keeps its first time and its
        alert."""
        verdicts = list(verdicts)
        if not verdicts:
            return
        now = clock.now().isoformat()
        with locked(self.ctx.state):
            entries = self._entries()
            for verdict in verdicts:
                old = entries.get(verdict.key)
                old = old if isinstance(old, dict) else {}
                entries[verdict.key] = {
                    "verdict": asdict(verdict), "stored": old.get("stored") or now,
                    "alerted": old.get("alerted")}
            self._write(entries)

    def mark_alerted(self, key: str, at: datetime) -> None:
        """Record that the alert of `key` went out at `at`, with or without a stored verdict."""
        when = (at if at.tzinfo else at.replace(tzinfo=UTC)).isoformat()
        with locked(self.ctx.state):
            entries = self._entries()
            entry = entries.get(key)
            if not isinstance(entry, dict):
                entry = {"verdict": None, "stored": when}
            entries[key] = {**entry, "alerted": when}
            self._write(entries)

    def alerted(self, key: str) -> bool:
        entry = self._entries().get(key)
        return isinstance(entry, dict) and _time(entry.get("alerted")) is not None

    def between(self, since: datetime, until: datetime) -> list[Verdict]:
        """The verdicts of the mail dated in [since, until), oldest first. A mail without a date
        is in no window."""
        start, end = (t if t.tzinfo else t.replace(tzinfo=UTC) for t in (since, until))
        found = []
        for entry in self._entries().values():
            verdict = _verdict(entry)
            dated = _time(verdict.date) if verdict else None
            if dated is not None and start <= dated < end:
                found.append((dated, verdict.key, verdict))
        found.sort(key=lambda row: (row[0], row[1]))
        return [verdict for _, _, verdict in found]

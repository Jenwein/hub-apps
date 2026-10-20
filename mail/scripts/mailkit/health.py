"""How the alert watcher is doing: when carillon was down, which mailboxes failed and how, and
when the last sweep ran. The user hears of each problem once, and the reports read the record.

`state/health.json` is written by the watcher and read by the reports and `alerts-status`. The
daily report also asks `mailbox_failed`, so a login problem is told once, by whichever of the two
sees it first. Every method takes the state lock for its read, or for its read and write; the lock
is not reentrant, so no method may be called while holding `paths.locked`.

A spell is the time from a failure to the recovery:

- carillon: from the moment the watcher finds it not running (it exited, could not be started, or
  the watcher before stopped without a word) until a carillon the watcher started has kept
  running; the spell then ends at the time that carillon started. A carillon that keeps failing
  soon after its start is one long spell. The user hears of it once per spell, when it has lasted
  longer than GRACE.
- a mailbox: from its first failure until `mailbox_ok`. A failure is of the kind `auth` (the login
  was refused), `connect` (the mailbox could not be checked) or `reader` (its new mail could not
  be sorted). `mailbox_failed` says when the user should hear of it: an `auth` or `reader` failure
  at once, a `connect` failure once it has lasted GRACE (a server that is away for a few minutes
  is no news), each kind once per spell. When the kind changes, the spell goes on; the record of
  problems gets an entry per kind.

`downtime` also counts the time since a heartbeat (`state/watch.json`) that is older than STALE:
a watcher that stopped that way (killed, or the machine went down) watched nothing since. A
watcher that is told to stop removes its heartbeat, so a stop that the user wanted is no downtime.

Spells that ended more than KEEP ago are dropped, so the monthly report can look back a month.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import clock
from .context import Ctx
from .paths import locked, read_json, write_json
from .settings import Mailbox, MailError

GRACE = timedelta(minutes=30)
KEEP = timedelta(days=62)
# A heartbeat this old means the watcher no longer runs. A tick normally writes one every few
# seconds, but a sweep that sorts a lot of mail may take a few minutes between two.
STALE = timedelta(minutes=20)
KINDS = ("auth", "connect", "reader")
HEALTH = "health.json"  # in the state directory
HEARTBEAT = "watch.json"  # in the state directory, written by the watcher


def _time(text: object) -> datetime | None:
    """An ISO 8601 time as an aware datetime (no offset: UTC), else None."""
    if not isinstance(text, str) or not text:
        return None
    try:
        found = datetime.fromisoformat(text)
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def _aware(at: datetime) -> datetime:
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _iso(at: datetime) -> str:
    return _aware(at).isoformat()


def sign_in_command(mb: Mailbox) -> str:
    """What the user types in the server's terminal, in the hub's folder, to sign `mb` in again."""
    command = "oauth-login" if mb.auth == "oauth" else "secret-set"
    return f"uv run apps/mail/scripts/mx.py {command} {mb.name}"


def heartbeat(state: Path) -> datetime | None:
    """When the watcher last said it was running, or None when there is no heartbeat (no watcher
    ran here, or the last one stopped when it was told) or it cannot be read."""
    try:
        data = read_json(Path(state) / HEARTBEAT, None)
    except MailError:  # being replaced right now (Windows), or not what the watcher wrote
        return None
    return _time(data.get("at")) if isinstance(data, dict) else None


def _covered(spans: Iterable[tuple[datetime, datetime]], since: datetime,
             until: datetime) -> timedelta:
    """How much of [since, until) the spans cover, each moment counted once."""
    cut = sorted(
        (max(start, since), min(end, until)) for start, end in spans
        if min(end, until) > max(start, since))
    total, reach = timedelta(0), since
    for start, end in cut:
        if end > reach:
            total += end - max(start, reach)
            reach = end
    return total


def _overlaps(entry: dict, since: datetime, until: datetime) -> bool:
    end = _time(entry["until"])
    return _time(entry["since"]) < until and (end is None or end > since)


def _valid(entry: object, *keys: str) -> bool:
    """Is `entry` a dict with a `since` time, an `until` time or None, and these string keys?"""
    return (
        isinstance(entry, dict) and _time(entry.get("since")) is not None
        and (entry.get("until") is None or _time(entry.get("until")) is not None)
        and all(isinstance(entry.get(key), str) for key in keys))


def _open(entries: list[dict], mailbox: str | None = None) -> dict | None:
    """The entry that has not ended (of `mailbox`), if there is one."""
    for entry in reversed(entries):
        if entry["until"] is None and (mailbox is None or entry["mailbox"] == mailbox):
            return entry
    return None


class Health:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.path = ctx.state / HEALTH

    def _load(self) -> dict:
        """The record as the file has it, with what is not a spell left out. Under the lock."""
        data = read_json(self.path, {})
        data = data if isinstance(data, dict) else {}
        carillon = data.get("carillon")
        problems = data.get("problems")
        spells = data.get("open")
        return {
            "carillon": [
                {**s, "told": s.get("told") is True}
                for s in (carillon if isinstance(carillon, list) else []) if _valid(s)],
            "problems": [
                p for p in (problems if isinstance(problems, list) else [])
                if _valid(p, "mailbox", "kind", "detail") and p["kind"] in KINDS],
            "open": {
                name: {"since": s["since"], "told": [k for k in s["told"] if k in KINDS]}
                for name, s in (spells.items() if isinstance(spells, dict) else ())
                if _valid(s) and isinstance(s.get("told"), list)},
            "last_sweep": data.get("last_sweep") if _time(data.get("last_sweep")) else None,
        }

    def _read(self) -> dict:
        with locked(self.ctx.state):
            return self._load()

    @contextmanager
    def _change(self) -> Iterator[dict]:
        """The record to change, under the lock; it is written when it changed."""
        with locked(self.ctx.state):
            data = self._load()
            before = json.dumps(data, sort_keys=True)
            yield data
            cutoff = clock.now() - KEEP
            for name in ("carillon", "problems"):
                data[name] = [
                    e for e in data[name] if e["until"] is None or _time(e["until"]) >= cutoff]
            if json.dumps(data, sort_keys=True) != before:
                write_json(self.path, data)

    # --- carillon ------------------------------------------------------------------------------

    def carillon_down(self, at: datetime) -> None:
        """carillon was found not running at `at`: a spell starts, unless one goes on already
        (it then starts at `at` if that is earlier)."""
        with self._change() as data:
            spell = _open(data["carillon"])
            if spell is None:
                data["carillon"].append({"since": _iso(at), "until": None, "told": False})
            elif _aware(at) < _time(spell["since"]):
                spell["since"] = _iso(at)

    def carillon_up(self, at: datetime) -> None:
        """A carillon that started at `at` has kept running: the spell ends at `at`."""
        with self._change() as data:
            spell = _open(data["carillon"])
            if spell is None:
                return
            since = _time(spell["since"])
            if _aware(at) <= since and not spell["told"]:
                data["carillon"].remove(spell)  # it was never down for any time
            else:
                spell["until"] = _iso(max(_aware(at), since))

    def carillon_notice_due(self, at: datetime) -> bool:
        """True once per down spell, when it has lasted longer than GRACE at `at`."""
        with self._change() as data:
            spell = _open(data["carillon"])
            if spell is None or spell["told"] or _aware(at) - _time(spell["since"]) <= GRACE:
                return False
            spell["told"] = True
            return True

    # --- mailboxes -----------------------------------------------------------------------------

    def mailbox_failed(self, name: str, kind: str, detail: str, at: datetime) -> bool:
        """Record a failure of the mailbox `name` at `at`; True when the user should hear of it
        now (see the module's docstring), and only once per spell and kind."""
        if kind not in KINDS:
            raise ValueError(f"unknown kind of failure {kind!r}")
        with self._change() as data:
            spell = data["open"].setdefault(name, {"since": _iso(at), "told": []})
            problem = _open(data["problems"], name)
            if problem is None or problem["kind"] != kind:
                if problem is not None:
                    problem["until"] = _iso(max(_aware(at), _time(problem["since"])))
                problem = {
                    "mailbox": name, "kind": kind, "since": _iso(at), "until": None,
                    "detail": detail}
                data["problems"].append(problem)
            else:
                problem["detail"] = detail
            if kind in spell["told"]:
                return False
            if kind == "connect" and _aware(at) - _time(problem["since"]) < GRACE:
                return False
            spell["told"].append(kind)
            return True

    def mailbox_ok(self, name: str, at: datetime, kinds: Iterable[str] = KINDS) -> None:
        """The mailbox `name` worked at `at`: its spell ends, when the failure that goes on is of
        one of `kinds` (a mailbox that answers says nothing about the reader). Nothing is said."""
        with self._change() as data:
            problem = _open(data["problems"], name)
            if problem is not None:
                if problem["kind"] not in kinds:
                    return
                problem["until"] = _iso(max(_aware(at), _time(problem["since"])))
            data["open"].pop(name, None)

    # --- what the reports read -----------------------------------------------------------------

    def downtime(self, since: datetime, until: datetime) -> timedelta:
        """How long within [since, until) carillon was down while alerts were on, the time since
        a stale heartbeat included (see the module's docstring)."""
        since, until = _aware(since), _aware(until)
        spans = [
            (_time(s["since"]), _time(s["until"]) or until) for s in self._read()["carillon"]]
        beat = heartbeat(self.ctx.state)
        if beat is not None and clock.now() - beat > STALE:
            spans.append((beat, until))
        return _covered(spans, since, until)

    def problems(self, since: datetime, until: datetime) -> list[dict]:
        """The mailbox problems that overlap [since, until), oldest first: `mailbox`, `kind`, and
        `since` and `until` as ISO 8601 text (`until` None while it goes on)."""
        since, until = _aware(since), _aware(until)
        return [
            {key: p[key] for key in ("mailbox", "kind", "since", "until")}
            for p in self._read()["problems"] if _overlaps(p, since, until)]

    def sweep_done(self, at: datetime) -> None:
        with self._change() as data:
            data["last_sweep"] = _iso(at)

    def last_sweep(self) -> datetime | None:
        return _time(self._read()["last_sweep"])

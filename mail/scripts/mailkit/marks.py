"""How far the alert watcher got in each inbox, and which mail came since (F-UID).

A mark is the inbox's UIDVALIDITY, the highest UID handled, and the time the newest handled mail
arrived (its INTERNALDATE). `fresh` starts with one `imap status` call: when nothing came, that is
all a sweep costs. New mail is the UIDs above the mark. When the server renumbered the folder (a
new UIDVALIDITY), UIDs mean nothing any more and the mark's time decides: the mail that arrived
after it. None of this may turn old mail into a storm of alerts: a first run only sets the mark
(the mail already there is not news), and after a long downtime only the newest MAX_CATCH_UP mails
are handed on, the rest counted as skipped.

`verdict_key` names a mail in the verdict store and in alerts with the UIDVALIDITY in it, so a UID
that a renumbered folder gives to another mail never finds the old mail's verdict.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta, timezone

from .context import Ctx
from .himalaya import Himalaya
from .paths import locked, read_json, write_json
from .settings import Mailbox

MAX_CATCH_UP = 200
MARKS = "marks.json"  # in the state directory
# The time zone where a day starts last. IMAP's SEARCH SINCE compares the date of each mail's
# INTERNALDATE in the server's own time zone, which may be a day behind the mark's: asking from
# the mark's date there misses nothing, and the exact time filters the rest.
LAST_ZONE = timezone(timedelta(hours=-12))


@dataclass
class Mark:
    uidvalidity: str
    last_uid: int
    last_date: str  # ISO 8601 with an offset: when the newest handled mail arrived


def verdict_key(mailbox: str, uidvalidity: str, id: str) -> str:
    return f"{mailbox}/{uidvalidity}/{id}"


def inbox_folder(mb: Mailbox) -> str:
    """The inbox as the server names it: the folder `fresh` looks at, and the folder of the
    items handed to `triage`."""
    return mb.folders.get("inbox") or "INBOX"


def uid_set(uids: Iterable[int]) -> str:
    """An IMAP UID set with every run of UIDs joined: `1:3,5,7:8`. A catch-up after a renumbering
    may name thousands of UIDs, and they go on a command line."""
    runs: list[list[int]] = []
    for uid in sorted(set(uids)):
        if runs and uid == runs[-1][1] + 1:
            runs[-1][1] = uid
        else:
            runs.append([uid, uid])
    return ",".join(str(a) if a == b else f"{a}:{b}" for a, b in runs)


def _aware(when: datetime) -> datetime:
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _mark_time(mark: Mark) -> datetime:
    return _aware(datetime.fromisoformat(mark.last_date))


def _moved_on(mark: Mark, uidvalidity: str, top: int,
              found: dict[int, datetime | None]) -> tuple[list[int], Mark, int]:
    """The UIDs of `found` to hand on, oldest first, the new mark and how many were skipped.

    Every UID up to `top` (UIDNEXT - 1 when the status was read) has been looked at: what is not
    in `found` is gone or is older than the mark, and no new mail gets a UID that low. So the new
    mark has the highest of `top` and the found UIDs, and the latest arrival of the mark and the
    found mail (the time of the newest handled mail; a server's clock can be wrong, but the mark
    never goes back)."""
    chosen = sorted(found)
    skipped = max(0, len(chosen) - MAX_CATCH_UP)
    chosen = chosen[skipped:]
    times = [_aware(when) for when in found.values() if when is not None]
    last_date = mark.last_date
    if times and max(times) > _mark_time(mark):
        last_date = max(times).isoformat()
    return chosen, Mark(uidvalidity, max([top, *found]), last_date), skipped


def fresh(hima: Himalaya, mb: Mailbox, mark: Mark | None,
          now: datetime) -> tuple[list[int], Mark, int]:
    """The UIDs of the inbox mail not handled yet, oldest first; the mark to save once they are
    handled; and how many newer mails were skipped (more than MAX_CATCH_UP came).

    - No mark: nothing; the mark is UIDNEXT - 1 and `now`.
    - The same UIDVALIDITY and nothing above the mark: nothing, after the one status call.
    - The same UIDVALIDITY: the UIDs above the mark (`<last + 1>:*` always answers the highest
      mail, even when nothing is newer, so only UIDs above the mark count).
    - Another UIDVALIDITY: the mail that arrived after the mark's time.
    """
    folder = inbox_folder(mb)
    uidvalidity, uid_next = hima.status(mb.name, folder)
    top = max(uid_next - 1, 0)  # UIDs start at 1: a mark of 0 asks for `1:*` later
    if mark is None:
        return [], Mark(uidvalidity, top, _aware(now).isoformat()), 0
    if uidvalidity == mark.uidvalidity:
        if top <= mark.last_uid:
            return [], mark, 0
        dates = hima.fetch_dates(mb.name, folder, f"{mark.last_uid + 1}:*")
        return _moved_on(mark, uidvalidity, top, {
            uid: when for uid, when in dates.items() if uid > mark.last_uid})
    since = _mark_time(mark)
    uids = hima.uids_since(mb.name, folder, since.astimezone(LAST_ZONE).date())
    dates = hima.fetch_dates(mb.name, folder, uid_set(uids)) if uids else {}
    asked = set(uids)
    return _moved_on(mark, uidvalidity, top, {
        uid: when for uid, when in dates.items()
        if uid in asked and when is not None and _aware(when) > since})


# --- the marks file ----------------------------------------------------------------------------


def _mark(entry: object) -> Mark | None:
    """The mark in a saved entry, or None when it is not one: a mailbox without a usable mark
    starts again from a baseline, which never alerts on old mail."""
    if not isinstance(entry, dict):
        return None
    uidvalidity, last_uid, last_date = (
        entry.get("uidvalidity"), entry.get("last_uid"), entry.get("last_date"))
    if not (isinstance(uidvalidity, str) and uidvalidity and isinstance(last_uid, int)
            and not isinstance(last_uid, bool) and isinstance(last_date, str)):
        return None
    try:
        datetime.fromisoformat(last_date)
    except ValueError:
        return None
    return Mark(uidvalidity, last_uid, last_date)


def load_marks(ctx: Ctx) -> dict[str, Mark]:
    """The saved marks by mailbox (`state/marks.json`); entries that are not marks are left out."""
    data = read_json(ctx.state / MARKS, {})
    found = {}
    for name, entry in (data.items() if isinstance(data, dict) else ()):
        mark = _mark(entry)
        if mark is not None:
            found[name] = mark
    return found


def save_marks(ctx: Ctx, marks: dict[str, Mark]) -> None:
    """Save these marks; the marks of other mailboxes stay. Takes the state lock, which is not
    reentrant: do not call it while holding `paths.locked`."""
    path = ctx.state / MARKS
    with locked(ctx.state):
        data = read_json(path, {})
        if not isinstance(data, dict):
            data = {}
        data.update({name: asdict(mark) for name, mark in marks.items()})
        write_json(path, data)

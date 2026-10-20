"""The mail app's sections of the hub's reports: `mx report daily` and `mx report monthly`.

cc-hub runs the section command from the hub root and pastes what it prints into the report as
it is: the agent never sees it. It passes the window as CC_HUB_REPORT_SINCE and
CC_HUB_REPORT_UNTIL, and moves the window on only once a report went out (or had nothing in it),
so a report that could not be sent is covered again by the next one. It cuts a section beyond
SECTION_LIMIT characters and stops one that runs longer than 300 s.

The daily section lists the inbox mail of every mailbox dated in the window: first the urgent
mail whose alert did not go out, then the daily tier by category, a line per mail, then the mail
the reader could not sort. A line holds the mailbox, the sender (from the headers, cleaned), the
reader's summary, and the deadline, amount and phishing warning when the reader found them: the
fields the reader checked, never text from a body. Verdicts that the watcher or an earlier report
stored are reused; the rest are sorted with `triage`, one mailbox at a time.

What one report could not finish, a later one picks up (`state/report.json`):

- `left`: mail that was not sorted in time, that the reader did not answer for, that could not be
  fetched, or whose mailbox failed while it was fetched. The next report sorts it with its own
  mail, whatever its date. Once listed, an entry keeps the end of the window that listed it
  (`listed`) until a report starts at or after that end: until then the report that listed it may
  not have gone out, and cc-hub covers its window again.
- `missed`: for a mailbox that could not be listed, where its window started. Later reports list
  it from there, until a report that read it went out (`read`), the same way. Each run marks
  every mailbox as missed before it starts, so a run that is stopped or fails halfway leaves the
  next one to cover its window.

A login problem is told once per failure spell, by this report or by the watcher, whichever sees
it first (`Health.mailbox_failed`); while the spell goes on, the report says the mailbox could
not be read. The state lock is never held across a call that takes it itself (triage, the
verdicts, health, the alert queue).
"""

from __future__ import annotations

import http.client
import json
import re
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import clock
from .alerts import AlertQueue
from .context import Ctx
from .health import Health, heartbeat, sign_in_command
from .himalaya import AuthError, Himalaya
from .marks import LAST_ZONE, inbox_folder, verdict_key
from .paths import locked, read_json, write_json
from .query import NAME_LIMIT, SUBJECT_LIMIT, Query, clean, server_query
from .reader import AMOUNT_LIMIT, SUMMARY_LIMIT, Reader, ReaderError, Verdict
from .settings import Mailbox, MailError
from .strings import t
from .tools import TOOLS, fetch_url
from .triage import FALLBACK_REASON, FETCH_FALLBACK_REASON, triage
from .verdicts import VerdictStore
from .watch import LOGIN_KINDS

# cc-hub stops a section after 300 s. No batch of mail starts sorting later than DAILY_BUDGET
# seconds after the section began (the listing before it counts too), so no batch starts after
# 150 s of the run even with uv's start-up. A batch fetches its mail within HIMALAYA_TIMEOUT and
# the reader answers within READER_TIMEOUT, so the last batch ends by about 280 s. Mail not
# sorted by then waits for the next report.
DAILY_BUDGET = 140.0
READER_TIMEOUT = 120.0
HIMALAYA_TIMEOUT = 20.0  # each himalaya call of the daily section
SEARCH_LIMIT = 500  # mails per mailbox that one daily section reads
SECTION_LIMIT = 20_000  # characters: cc-hub cuts a section's output beyond this
LATEST_TIMEOUT = 10  # seconds GitHub has to say a tool's latest release
FIELD_LIMIT = 300  # an error text as part of a line
REPORT = "report.json"  # in the state directory
# The categories of the daily tier that have a heading of their own, in the order shown; the
# others (money, security, other) are shown under "other".
GROUPS = ("awaiting_reply", "due_soon", "orders", "bills")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,39}")
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

Item = tuple[str, str, str, int]  # verdict key, mailbox, folder, UID: what triage takes


def _aware(at: datetime) -> datetime:
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _time(text: object) -> datetime | None:
    """An ISO 8601 time as an aware datetime (no offset: UTC), else None."""
    if not isinstance(text, str) or not text:
        return None
    try:
        return _aware(datetime.fromisoformat(text))
    except ValueError:
        return None


def _text(value: object) -> str:
    """A field of a stored verdict as text: the store is a file, and could hold anything."""
    return value if isinstance(value, str) else ""


# --- the window --------------------------------------------------------------------------------


def _env_time(environ: Mapping[str, str], name: str) -> datetime:
    text = environ.get(name)
    if not text:
        raise MailError(f"run by cc-hub's report: {name} is not set")
    try:
        found = datetime.fromisoformat(text)
    except ValueError:
        raise MailError(f"{name} is not an ISO 8601 time: {clean(text, 60)!r}") from None
    return found if found.tzinfo else found.astimezone()


def env_window(environ: Mapping[str, str]) -> tuple[datetime, datetime]:
    """The window [since, until) that cc-hub's report passes in the environment."""
    return _env_time(environ, "CC_HUB_REPORT_SINCE"), _env_time(environ, "CC_HUB_REPORT_UNTIL")


# --- what a report leaves for the next ---------------------------------------------------------


def _done(text: object, since: datetime) -> bool:
    """Did a report that ended at `text` go out? Then the next one starts at or after its end."""
    end = _time(text)
    return end is not None and end <= since


def _left_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    key, mailbox, folder, uid = (entry.get(k) for k in ("key", "mailbox", "folder", "uid"))
    if not (isinstance(key, str) and isinstance(mailbox, str) and isinstance(folder, str)
            and isinstance(uid, int) and not isinstance(uid, bool)):
        return False
    parts = key.split("/")
    listed = entry.get("listed")
    return (len(parts) == 3 and parts[0] == mailbox and parts[2] == str(uid)
            and (listed is None or _time(listed) is not None))


def _missed_entry(entry: object) -> bool:
    return (isinstance(entry, dict) and _time(entry.get("since")) is not None
            and (entry.get("read") is None or _time(entry.get("read")) is not None))


def _load_state(ctx: Ctx, since: datetime) -> dict:
    """What earlier reports left, without what a report that went out has listed or read. A
    file that cannot be read counts as none: it must not stop every report from now on."""
    with locked(ctx.state):
        try:
            data = read_json(ctx.state / REPORT, {})
        except MailError:
            data = {}
    data = data if isinstance(data, dict) else {}
    left, missed = data.get("left"), data.get("missed")
    return {
        "left": [
            e for e in (left if isinstance(left, list) else [])
            if _left_entry(e) and not _done(e.get("listed"), since)],
        "missed": {
            name: e for name, e in (missed.items() if isinstance(missed, dict) else ())
            if _missed_entry(e) and not _done(e.get("read"), since)},
    }


def _save_state(ctx: Ctx, state: dict) -> None:
    with locked(ctx.state):
        write_json(ctx.state / REPORT, state)


# --- the daily section -------------------------------------------------------------------------


@dataclass
class _Box:
    """One mailbox in the daily section."""

    mb: Mailbox
    folder: str
    start: datetime  # where its window starts: `since`, or earlier when a report missed it
    validity: str | None = None  # its UIDVALIDITY, once it was listed
    items: list[Item] = field(default_factory=list)  # to sort: carried first, then the new mail
    carried: set[str] = field(default_factory=set)  # the keys of the carried entries in `items`
    full: bool = False  # the search found SEARCH_LIMIT mails: there may be older ones
    error: MailError | None = None  # it could not be listed, or fetched from


def _list(hima: Himalaya, box: _Box, until: datetime, left: list[dict]) -> None:
    """Fill in `box`: the mail carried for it that the server still numbers the same way, then
    its mail dated in [box.start, until), oldest first. A failure goes into `box.error`."""
    name = box.mb.name
    try:
        validity, _ = hima.status(name, box.folder)
        # `after D` compares the day of each Date header in the sender's own zone: the day
        # before the start in the zone where a day starts last misses nothing, and the exact
        # time filters the rest.
        day = box.start.astimezone(LAST_ZONE).date()
        found = hima.search(name, box.folder, server_query(Query(since=day)), SEARCH_LIMIT)
    except MailError as err:
        box.error = err
        return
    box.validity = validity
    for entry in left:
        if (entry["mailbox"] == name and entry["folder"] == box.folder
                and entry["key"].split("/")[1] == validity):
            box.items.append((entry["key"], name, box.folder, entry["uid"]))
            box.carried.add(entry["key"])
    dated = [(_aware(e.date), e) for e in found if e.date is not None]
    box.full = len(found) >= SEARCH_LIMIT and bool(dated) and min(d for d, _ in dated) >= box.start
    new = sorted(
        (when, int(e.id)) for when, e in dated
        if box.start <= when < until and e.id.isascii() and e.id.isdigit())
    known = {item[0] for item in box.items}
    for _, uid in new:
        key = verdict_key(name, validity, str(uid))
        if key not in known:
            known.add(key)
            box.items.append((key, name, box.folder, uid))


def _fell_back(v: Verdict, reason: str) -> bool:
    """Is `v` the fallback verdict of triage with this reason (no summary from the reader)?"""
    return v.reason == reason and not _text(v.summary)


def _order(v: Verdict) -> tuple:
    when = _time(v.date)
    return (when is None, when or EPOCH, v.key)


def _who(v: Verdict, language: str) -> str:
    return clean(_text(v.sender), NAME_LIMIT) or t("unknown", language)


def _mail_line(name: str, v: Verdict, language: str) -> str:
    about = (clean(_text(v.summary), SUMMARY_LIMIT) or clean(_text(v.subject), SUBJECT_LIMIT)
             or t("unknown", language))
    parts = [f"- [{name}] {_who(v, language)}: {about}"]
    deadline = _text(v.deadline)
    if DAY.fullmatch(deadline):
        parts.append(deadline)
    amount = clean(_text(v.amount), AMOUNT_LIMIT)
    if amount:
        parts.append(amount)
    if v.phishing is True:
        parts.append(f"⚠ {t('phishing', language)}")
    return " · ".join(parts)


def _unsorted_line(name: str, v: Verdict, language: str) -> str:
    subject = clean(_text(v.subject), SUBJECT_LIMIT) or t("unknown", language)
    return f"- [{name}] {_who(v, language)}: {subject}"


def _cut(blocks: list[tuple[str, list[str]]], room: int) -> tuple[list[str], int]:
    """The blocks (a heading and its lines) as text, joined by blank lines in at most `room`
    characters, and how many lines did not fit. A heading goes only with a line of its own."""
    out: list[str] = []
    length = -2  # no blank line before the first block
    for at, (heading, rows) in enumerate(blocks):
        text = heading
        for n, row in enumerate(rows):
            if length + 2 + len(text) + 1 + len(row) > room:
                if n:
                    out.append(text)
                return out, len(rows) - n + sum(len(r) for _, r in blocks[at + 1:])
            text += "\n" + row
        out.append(text)
        length += 2 + len(text)
    return out, 0


def _section(blocks: list[tuple[str, list[str]]], tail: list[str], language: str) -> str:
    """The mail blocks, then the tail, within SECTION_LIMIT: mail lines that do not fit are
    counted in one line instead, and the tail is always there."""
    whole = "\n\n".join([*(f"{h}\n" + "\n".join(rows) for h, rows in blocks), *tail])
    if len(whole) <= SECTION_LIMIT:
        return whole
    total = sum(len(rows) for _, rows in blocks)
    more = t("more_mail", language, n=total)  # as long as the line can get
    room = SECTION_LIMIT - len("\n\n".join([more, *tail])) - 2
    shown, missing = _cut(blocks, room)
    return "\n\n".join([*shown, t("more_mail", language, n=missing), *tail])


def _minutes(span: timedelta) -> int:
    return round(span.total_seconds() / 60)


def _duration(minutes: int, language: str) -> str:
    days, rest = divmod(minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    return " ".join(
        t(key, language, n=n)
        for key, n in (("duration_days", days), ("duration_hours", hours),
                       ("duration_minutes", minutes)) if n)


def _alert_lines(ctx: Ctx, since: datetime, until: datetime, language: str,
                 month: bool = False) -> list[str]:
    """How alerts did in the window: down for how long (or ran all the time), not running now,
    and, for the daily section, the urgent mails alerted late and those that still wait."""
    lines = []
    down = _minutes(Health(ctx).downtime(since, until))
    running = heartbeat(ctx.state) is not None
    if down:
        key = "alerts_down_month" if month else "alerts_down"
        lines.append(t(key, language, duration=_duration(down, language)))
    elif running:
        lines.append(t("alerts_ran_month" if month else "alerts_ran", language))
    if not running:
        lines.append(t("alerts_stopped", language))
    if month:
        return lines
    queue = AlertQueue(ctx)
    late = {key for entry in queue.late(since, until) for key in entry["keys"]}
    if late:
        lines.append(t("alerts_late", language, n=len(late)))
    waiting = {key for entry in queue.pending() for key in entry["keys"]}
    if waiting:
        lines.append(t("alerts_queued", language, n=len(waiting)))
    return lines


def _problem(health: Health, box: _Box, at: datetime, language: str) -> str:
    """The line of a mailbox that failed. The fix of a login problem only once per spell."""
    why = clean(str(box.error), FIELD_LIMIT)
    if isinstance(box.error, AuthError):
        if health.mailbox_failed(box.mb.name, "auth", why, at):
            return t("login_problem", language, mailbox=box.mb.name, fix=sign_in_command(box.mb))
    else:
        health.mailbox_failed(box.mb.name, "connect", why, at)
    return t("cannot_read", language, mailbox=box.mb.name, why=why)


def daily(ctx: Ctx, hima: Himalaya, reader: Reader, since: datetime, until: datetime,
          language: str, *, monotonic: Callable[[], float] = time.monotonic) -> str:
    """The daily section for [since, until), in `language` (see the module's docstring)."""
    start = monotonic()
    since, until = _aware(since), _aware(until)
    until_text = until.isoformat()
    state = _load_state(ctx, since)
    boxes = []
    for mb in ctx.mailboxes.values():
        missed = state["missed"].get(mb.name)
        begin = min(since, _time(missed["since"])) if missed else since
        boxes.append(_Box(mb, inbox_folder(mb), begin))
    # Until this run has finished, every mailbox counts as missed from where it starts.
    _save_state(ctx, {
        "left": state["left"],
        "missed": {box.mb.name: {"since": box.start.isoformat(), "read": None} for box in boxes}})

    with ThreadPoolExecutor(max(1, len(boxes))) as pool:  # a slow server delays only its own
        list(pool.map(lambda box: _list(hima, box, until, state["left"]), boxes))

    store = VerdictStore(ctx)
    verdicts: list[Verdict] = []
    carry: set[str] = set()  # what the next report takes
    unfetched: dict[str, int] = {}
    not_sorted = reader_failed = 0
    reader_why: str | None = None
    todo = [box for box in boxes if box.error is None and box.items]
    for index, box in enumerate(todo):
        # Each mailbox may start batches for its share of the budget that is left, so the first
        # does not take it all; once the reader did not answer, it is not asked again.
        budget = 0.0 if reader_why is not None else max(
            0.0, (DAILY_BUDGET - (monotonic() - start)) / (len(todo) - index))
        keys = [item[0] for item in box.items]
        try:
            result = triage(ctx, hima, reader, box.items, budget=budget, monotonic=monotonic)
        except MailError as err:
            # What finished before the error was stored; the rest waits for the next report.
            found = store.get_many(keys)
            verdicts += [found[key] for key in keys if key in found]
            missing = [key for key in keys if key not in found]
            carry.update(missing)
            if isinstance(err, ReaderError):
                reader_why = clean(str(err), FIELD_LIMIT)
                reader_failed += len(missing)
            else:  # the mailbox failed while its mail was fetched
                box.error = err
            continue
        for v in result.verdicts:
            verdicts.append(v)
            if _fell_back(v, FETCH_FALLBACK_REASON):  # not stored: fetched again next time
                carry.add(v.key)
                unfetched[box.mb.name] = unfetched.get(box.mb.name, 0) + 1
        carry.update(result.left)
        if reader_why is None:
            not_sorted += len(result.left)
        else:
            reader_failed += len(result.left)

    owner = {item[0]: item[1] for box in boxes for item in box.items}
    urgent: list[str] = []
    grouped: dict[str, list[str]] = {group: [] for group in (*GROUPS, "other")}
    unsorted: list[str] = []
    for v in sorted(verdicts, key=_order):
        name = owner[v.key]
        if _fell_back(v, FETCH_FALLBACK_REASON):
            continue  # counted per mailbox
        if _fell_back(v, FALLBACK_REASON):
            unsorted.append(_unsorted_line(name, v, language))
        elif v.tier == "now":
            if not store.alerted(v.key):
                urgent.append(_mail_line(name, v, language))
        elif v.tier == "daily":
            group = v.category if v.category in GROUPS else "other"
            grouped[group].append(_mail_line(name, v, language))
    blocks = [(t(key, language), rows) for key, rows in (
        ("urgent", urgent), *grouped.items(), ("unsorted", unsorted)) if rows]

    tail = [t("unfetched", language, mailbox=name, n=n) for name, n in unfetched.items()]
    if not_sorted:
        tail.append(t("not_sorted", language, n=not_sorted))
    if reader_failed:
        tail.append(t("reader_failed", language, why=reader_why, n=reader_failed))
    health, at = Health(ctx), clock.now()
    for box in boxes:
        if box.error is not None:
            tail.append(_problem(health, box, at, language))
        else:
            health.mailbox_ok(box.mb.name, at, kinds=LOGIN_KINDS)
            if box.full:
                tail.append(t("over_limit", language, mailbox=box.mb.name, n=SEARCH_LIMIT))
    if ctx.settings.alerts_enabled:
        tail += _alert_lines(ctx, since, until, language)
    if not blocks and not (unfetched or not_sorted or reader_failed):
        tail.insert(0, t("daily_nothing", language))

    _save_state(ctx, _next_state(state, boxes, carry, {v.key for v in verdicts}, until_text))
    return _section(blocks, tail, language)


def _next_state(state: dict, boxes: list[_Box], carry: set[str], got: set[str],
                until: str) -> dict:
    """What this run leaves for the next (see the module's docstring)."""
    by_name = {box.mb.name: box for box in boxes}
    left: list[dict] = []
    kept: set[str] = set()
    for entry in state["left"]:
        box = by_name.get(entry["mailbox"])
        if box is None:
            continue  # the mailbox is gone
        key = entry["key"]
        if key in kept:
            continue
        if box.validity is None:
            left.append(entry)  # its mailbox was not listed: it waits as it was
        elif key in box.carried and (key in carry or key in got):
            left.append({**entry, "listed": None if key in carry else until})
        else:
            continue  # the server no longer has it, or numbers its mail anew
        kept.add(key)
    for box in boxes:
        for key, mailbox, folder, uid in box.items:
            if key in carry and key not in kept:
                kept.add(key)
                left.append(
                    {"key": key, "mailbox": mailbox, "folder": folder, "uid": uid, "listed": None})
    missed = {}
    for box in boxes:
        name = box.mb.name
        if box.validity is None:
            missed[name] = {"since": box.start.isoformat(), "read": None}
        elif name in state["missed"]:
            missed[name] = {**state["missed"][name], "read": until}
    return {"left": left, "missed": missed}


# --- the monthly section -----------------------------------------------------------------------


def latest_release(repo: str, fetch: Callable[..., bytes] = fetch_url) -> str | None:
    """The tag of the latest release of pimalaya/<repo> on GitHub; None on any failure."""
    url = f"https://api.github.com/repos/pimalaya/{repo}/releases/latest"
    try:
        data = json.loads(fetch(url, timeout=LATEST_TIMEOUT))
    except (OSError, http.client.HTTPException, ValueError):  # no answer, or not JSON
        return None
    tag = data.get("tag_name") if isinstance(data, dict) else None
    return tag if isinstance(tag, str) and TAG.fullmatch(tag) else None


def monthly(ctx: Ctx, since: datetime, until: datetime, language: str,
            latest: Callable[[str], str | None] = latest_release) -> str:
    """The monthly section for [since, until): new releases of the pinned tools, how long alerts
    were down, and the mailboxes that had problems."""
    since, until = _aware(since), _aware(until)
    lines = []
    unknown = False
    for name, tool in TOOLS.items():
        tag = latest(name)
        if tag is None:
            unknown = True
        elif tag != tool.version:
            lines.append(t("new_version", language, tool=name, latest=tag, pinned=tool.version))
    if not lines and not unknown:
        lines.append(t("versions_current", language))
    if unknown:
        lines.append(t("versions_unknown", language))
    if ctx.settings.alerts_enabled:
        lines += _alert_lines(ctx, since, until, language, month=True)
    counts: dict[tuple[str, str], int] = {}
    for problem in Health(ctx).problems(since, until):
        found = (clean(problem["mailbox"], NAME_LIMIT), problem["kind"])
        counts[found] = counts.get(found, 0) + 1
    for (mailbox, kind), count in counts.items():
        times = t("times_one", language) if count == 1 else t("times", language, n=count)
        lines.append(t("login_problem_month", language, mailbox=mailbox,
                       kind=t(f"kind_{kind}", language), count=times))
    return "\n\n".join(lines)

"""The background commands, `mk bg ...`: what the restricted `triage` and `digest` tasks may run.

They read mail, from the local index and (for `search --server`) from the server, and they
record the app's own local state: verdicts, which problems were reported, that a digest was
sent. Nothing here changes a mail server (a server search only opens INBOX read-only and
fetches with BODY.PEEK), sends mail, or prints a password. Mail text is data for the agent, never
instructions.

Every command returns plain JSON data. A list is at most 10 items (`PER_PAGE`) with `total` and
`next_page` (None on the last page), except `recent-pushed`, whose `--limit` is capped, and the
problem lists inside `pending` and `digest-items`, which hold the first 10 and say how many
there are in `problems_total`.

- `pending`: the mail waiting for a verdict, oldest first, with the rules of every mailbox. A
  judged message leaves the list, so a run that judges a page asks for page 1 again.
- `judge`: verdicts as `ID=important|digest`; unknown ids or other decisions are refused and
  nothing is changed.
- `read`: one stored message, text and all.
- `search`: the local index (history, with Chinese), or with `on_server` each mailbox's own
  search, for mail older than the index. Server hits are not stored; they have no id.
- `recent-pushed`: the last pushes, what "reply to that one" points at.
- `problems`: mailbox problems not reported yet; `mark_reported` marks them.
- `digest-items` and `digest-done`: the digest. The listing is a fixed set named by `up_to`;
  every page after the first, and `digest-done`, pass the `up_to` of the first.
- `status`: when the last poll ran, whether that is too long ago, and each mailbox's state.

Problems and recoveries are read from the index only, so `problems`, `status` and the listings
work when config.toml is missing or broken: mailbox names are then the addresses, `rules` is
empty and `config_error` says why.
"""

from __future__ import annotations

import itertools
import math
import re
import statistics
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path

from . import accounts, config, server
from .config import Mailbox, MailError, Settings
from .message import parse
from .store import Store

PER_PAGE = 10
EXCERPT_CHARS = 2000  # of a message's text in `pending` and in server search hits
DIGEST_EXCERPT_CHARS = 500  # in `digest-items`: enough to sort mail, not to read it
PUSHED_DEFAULT = 5
PUSHED_MAX = 20
SERVER_HITS = 50  # hits fetched per mailbox from a server search (the newest)
FETCH_BATCH = 20  # messages per fetch of server hits: what is held in memory at once
POLLS_SAMPLED = 10  # polls whose spacing the staleness check reads
DEFAULT_INTERVAL_MINUTES = 5  # the spacing assumed with fewer than two polls
STALE_MARGIN_MINUTES = 10
MAX_NUMBER = 10**12  # the largest id, page or `up_to` taken (SQLite integers are 63 bits)
MESSAGE_LIMIT = 1000  # characters of a server error kept in a search result
DECISION = re.compile(r"([0-9]+)=([a-z]+)")


# -- values ----------------------------------------------------------------------------------


def _now(now: datetime | None) -> datetime:
    now = now or datetime.now(UTC)
    return now if now.tzinfo else now.replace(tzinfo=UTC)


def _utc_iso(value: str | None) -> str | None:
    """An ISO-8601 time as UTC with whole seconds, or None when it is none or not a time."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _check_number(value: int, what: str, minimum: int = 1) -> None:
    if not minimum <= value <= MAX_NUMBER:
        raise MailError(f"{what} must be a number from {minimum} to {MAX_NUMBER}, not {value}")


def _check_page(page: int) -> None:
    _check_number(page, "page")


def _day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise MailError(f"since must be a date like 2026-10-01, not {text!r}") from None


def _settings(mail_dir: Path) -> tuple[Settings | None, str | None]:
    """The settings, or (None, why they cannot be read): the background commands answer
    from the index when config.toml is missing or broken."""
    try:
        return config.load(mail_dir), None
    except MailError as err:
        return None, str(err)


def _find_mailbox(settings: Settings | None, config_error: str | None, name: str) -> Mailbox:
    if settings is None:
        raise MailError(f"cannot look up the mailbox {name!r}: {config_error}")
    box = settings.mailboxes.get(name)
    if box is None:
        raise MailError(f"there is no mailbox named {name!r}")
    return box


def _lookup(settings: Settings | None, address: str) -> str | None:
    if settings is not None:
        for box in settings.mailboxes.values():
            if accounts.same_address(box.address, address):
                return box.name
    return None


def _name(settings: Settings | None, address: str) -> str:
    """The short name of the mailbox at `address`; the address itself when there is none (no
    config, or a mailbox that was edited out)."""
    return _lookup(settings, address) or address


def _rules(settings: Settings | None) -> dict:
    if settings is None:
        return {}
    return {name: config.effective(settings, name) for name in settings.mailboxes}


def _paged(items: list, page: int) -> tuple[list, int | None]:
    start = (page - 1) * PER_PAGE
    return items[start:start + PER_PAGE], page + 1 if start + PER_PAGE < len(items) else None


# -- the shapes ------------------------------------------------------------------------------


def _attachments(found: list[dict]) -> list[dict]:
    return [{"index": a["index"], "filename": a["filename"], "size": a["size"]} for a in found]


def _item(message: dict, settings: Settings | None, excerpt: int) -> dict:
    """A message as `pending` and `digest-items` list it."""
    return {
        "id": message["id"], "mailbox": _name(settings, message["address"]),
        "from": message["from_addr"], "from_name": message["from_name"],
        "addressed": message["addressed"], "subject": message["subject"],
        "date": message["date"], "excerpt": message["text"][:excerpt],
        "attachments": _attachments(message["attachments"]), "bulk": message["bulk"],
    }


def _summary(row: dict, settings: Settings | None) -> dict:
    """A message as `search` and `recent-pushed` list it."""
    return {
        "id": row["id"], "mailbox": _name(settings, row["address"]), "from": row["from_addr"],
        "from_name": row["from_name"], "subject": row["subject"], "date": row["date"],
        "status": row["status"], "attachments": _attachments(row["attachments"]),
    }


def _full(message: dict, settings: Settings | None) -> dict:
    return {
        "id": message["id"], "mailbox": _name(settings, message["address"]),
        "address": message["address"], "status": message["status"], "date": message["date"],
        "date_original": message["date_original"], "from": message["from_addr"],
        "from_name": message["from_name"], "to": message["to"], "cc": message["cc"],
        "subject": message["subject"], "text": message["text"],
        "attachments": message["attachments"], "bulk": message["bulk"],
        "addressed": message["addressed"], "message_id": message["message_id"],
        "in_reply_to": message["in_reply_to"], "references": message["references"],
        "uid": message["uid"], "uidvalidity": message["uidvalidity"],
        "added_at": message["added_at"], "judged_at": message["judged_at"],
        "pushed_at": message["pushed_at"], "digested_at": message["digested_at"],
    }


def _problem_entry(problem: dict, settings: Settings | None) -> dict:
    return {
        "ids": [problem["id"]], "mailbox": _lookup(settings, problem["address"]),
        "address": problem["address"], "kind": problem["kind"], "state": problem["state"],
        "message": problem["message"], "first_seen": problem["first_seen"],
        "last_seen": problem["last_seen"], "resolved_at": problem["resolved_at"],
        "reported_at": problem["reported_at"], "count": 1,
    }


def group_problems(raw: list[dict], settings: Settings | None) -> list[dict]:
    """The entries of `Store.problems` as items to tell the user about. The recoveries of one
    mailbox and kind (a connection that came and went) are one item that keeps all their ids,
    so that marking it reported marks them all; an open problem is its own item (a mailbox has
    one open problem per kind). `mailbox` is None for what is no mailbox (`config`)."""
    entries: list[dict] = []
    recovered: dict[tuple[str, str], dict] = {}
    for problem in raw:
        key = (problem["address"], problem["kind"])
        if problem["state"] == "recovered" and key in recovered:
            into = recovered[key]
            into["ids"].append(problem["id"])
            into["count"] += 1
            into["message"] = problem["message"]
            into["first_seen"] = min(into["first_seen"], problem["first_seen"])
            into["last_seen"] = max(into["last_seen"], problem["last_seen"])
            into["resolved_at"] = max(into["resolved_at"], problem["resolved_at"])
            into["reported_at"] = max(
                (t for t in (into["reported_at"], problem["reported_at"]) if t), default=None)
            continue
        entry = _problem_entry(problem, settings)
        entries.append(entry)
        if problem["state"] == "recovered":
            recovered[key] = entry
    return entries


def _problem_list(raw: list[dict], settings: Settings | None) -> tuple[list[dict], int]:
    """The first page of the grouped problems, and how many there are."""
    entries = group_problems(raw, settings)
    return entries[:PER_PAGE], len(entries)


# -- pending, judge, read --------------------------------------------------------------------


def pending(mail_dir: Path, *, page: int = 1) -> dict:
    """`{"rules": {mailbox: effective rules}, "config_error", "items": [...], "total",
    "next_page", "problems": [not reported yet], "problems_total"}`. Without a readable
    config.toml `rules` is empty and `config_error` says why; items and problems still come."""
    _check_page(page)
    settings, config_error = _settings(mail_dir)
    with Store.open(mail_dir) as store:
        listing = store.pending(limit=PER_PAGE, page=page)
        raw = store.problems(unreported_only=True)
    problems, problems_total = _problem_list(raw, settings)
    return {
        "rules": _rules(settings), "config_error": config_error,
        "items": [_item(m, settings, EXCERPT_CHARS) for m in listing["items"]],
        "total": listing["total"], "next_page": listing["next_page"],
        "problems": problems, "problems_total": problems_total,
    }


def parse_verdicts(tokens: list[str]) -> dict[int, str]:
    """`["12=important", "13=digest"]` as `{12: "important", 13: "digest"}`. Whether the
    decision is a known one, and the id a known message, is for `Store.judge` to say."""
    if not tokens:
        raise MailError("give at least one ID=DECISION, such as 12=important or 13=digest")
    verdicts: dict[int, str] = {}
    for token in tokens:
        found = DECISION.fullmatch(token)
        if found is None:
            raise MailError(
                f"{token!r} is not ID=DECISION, such as 12=important or 13=digest")
        mid, decision = int(found.group(1)), found.group(2)
        _check_id(mid)
        if verdicts.setdefault(mid, decision) != decision:
            raise MailError(f"message {mid} is given two different decisions")
    return verdicts


def _check_id(mid: int) -> None:
    if not 0 <= mid <= MAX_NUMBER:
        raise MailError(f"no message with id {mid}")


def judge(mail_dir: Path, decisions: list[str], *, now: datetime | None = None) -> dict:
    """Give verdicts (`ID=important` or `ID=digest`). `{"judged": n, "pending_left": m}`:
    `judged` counts the messages that were pending; one that is not any more is left alone."""
    verdicts = parse_verdicts(decisions)
    with Store.open(mail_dir) as store:
        judged = store.judge(verdicts, _now(now))
        left = store.pending(limit=1)["total"]
    return {"judged": judged, "pending_left": left}


def read(mail_dir: Path, mid: int) -> dict:
    """The whole stored message, text untruncated, with its attachments' details."""
    _check_id(mid)
    settings, _ = _settings(mail_dir)
    with Store.open(mail_dir) as store:
        return _full(store.message(mid), settings)


# -- search ----------------------------------------------------------------------------------


def search(
    hub: Path, mail_dir: Path, text: str = "", *, mailbox: str | None = None,
    sender: str | None = None, since: str | None = None, page: int = 1,
    on_server: bool = False, connect=None,
) -> dict:
    """Find mail: `{"items", "total", "next_page", "source", "errors"}`.

    Locally (`source` "local") `text` is one substring of the subject, sender or text, `sender`
    a substring of the From address or name, `since` a date `YYYY-MM-DD`, `mailbox` a short
    name. With `on_server` (`source` "server", plus `capped`) every mailbox, or `mailbox`, is
    searched with its server's own search for `text` (required), the newest `SERVER_HITS` hits
    are fetched read-only and listed with an `excerpt` and no `id`: they are not stored, for
    mail older than the index. A mailbox that cannot answer is an entry of `errors`
    (`{"mailbox", "error"}`) and the others still answer. `capped` names the mailboxes that had
    more hits than were fetched, with their number of hits."""
    _check_page(page)
    day = _day(since) if since else None
    settings, config_error = _settings(mail_dir)
    box = _find_mailbox(settings, config_error, mailbox) if mailbox is not None else None
    if on_server:
        if settings is None:
            raise MailError(config_error)
        return _search_server(
            hub, [box] if box else list(settings.mailboxes.values()), text, day, sender, page,
            connect)
    with Store.open(mail_dir) as store:
        found = store.search(
            text, address=box.address if box else None, sender=sender, since=day, page=page,
            per_page=PER_PAGE)
    return {
        "items": [_summary(m, settings) for m in found["items"]], "total": found["total"],
        "next_page": found["next_page"], "source": "local", "errors": [],
    }


def _scrub(text: str, password: str | None) -> str:
    if password:
        text = text.replace(password, "[password]")
    return text[:MESSAGE_LIMIT]


def _hit(name: str, uid: int, raw: bytes) -> dict:
    parsed = parse(raw)
    return {
        "id": None, "mailbox": name, "uid": uid, "from": parsed.from_addr,
        "from_name": parsed.from_name, "subject": parsed.subject, "date": _utc_iso(parsed.date),
        "excerpt": parsed.text[:EXCERPT_CHARS],
        "attachments": _attachments([asdict(a) for a in parsed.attachments]),
    }


def _from_sender(hit: dict, sender: str) -> bool:
    needle = sender.strip().lower()
    return needle in hit["from"].lower() or needle in hit["from_name"].lower()


def _from_server(mb: Mailbox, password: str, text: str, day: date | None, sender: str | None,
                 connect) -> tuple[list[dict], int]:
    """The hits of one mailbox (the newest `SERVER_HITS`; the highest UIDs arrived last) and
    how many hits the server found."""
    srv = connect(mb, password)
    try:
        uids = sorted(set(srv.search_text(text, day)), reverse=True)
        wanted = uids[:SERVER_HITS]
        hits = []
        for start in range(0, len(wanted), FETCH_BATCH):
            for uid, raw in srv.fetch(wanted[start:start + FETCH_BATCH]):
                hit = _hit(mb.name, uid, raw)
                if sender is None or _from_sender(hit, sender):
                    hits.append(hit)
    finally:
        srv.close()
    return hits, len(uids)


def _search_server(hub: Path, boxes: list[Mailbox], text: str, day: date | None,
                   sender: str | None, page: int, connect) -> dict:
    if not text.strip():
        raise MailError("a search on the server needs the search text")
    connect = connect or server.connect
    items: list[dict] = []
    errors: list[dict] = []
    capped: dict[str, int] = {}
    polled: list[str] = []
    for mb in boxes:
        if any(accounts.same_address(mb.address, other) for other in polled):
            continue  # two sections for one address (a hand edit): one inbox
        polled.append(mb.address)
        password = None
        try:
            password = accounts.read_password(hub, mb.address)
            hits, total = _from_server(mb, password, text, day, sender, connect)
        except Exception as err:  # noqa: BLE001 - one mailbox must not stop the others
            detail = str(err) if isinstance(err, MailError) else f"{type(err).__name__}: {err}"
            errors.append({"mailbox": mb.name, "error": _scrub(detail, password)})
            continue
        items += hits
        if total > SERVER_HITS:
            capped[mb.name] = total
    items.sort(key=lambda hit: (hit["date"] or "", hit["uid"]), reverse=True)
    shown, next_page = _paged(items, page)
    return {"items": shown, "total": len(items), "next_page": next_page, "source": "server",
            "errors": errors, "capped": capped}


# -- pushes and problems ---------------------------------------------------------------------


def recent_pushed(mail_dir: Path, *, limit: int | None = None) -> dict:
    """The last pushes, newest first: `{"items", "total", "limit"}`. `limit` (default 5) is
    capped at 20; `total` is how many pushes the index holds."""
    limit = PUSHED_DEFAULT if limit is None else limit
    if limit < 1:
        raise MailError("the limit must be at least 1")
    limit = min(limit, PUSHED_MAX)
    settings, _ = _settings(mail_dir)
    with Store.open(mail_dir) as store:
        pushes = store.pushes(limit)
        total = store.push_count()
    return {
        "items": [{**_summary(p, settings), "pushed_at": p["pushed_at"]} for p in pushes],
        "total": total, "limit": limit,
    }


def problems(mail_dir: Path, *, mark_reported: list[int] | None = None, page: int = 1,
             now: datetime | None = None) -> dict:
    """The mailbox problems not reported yet, `{"problems", "total", "next_page"}`: an open
    problem, or a recovery (all the recoveries of one mailbox and kind as one item). With
    `mark_reported` (the `ids` of items; unknown ones are ignored) those are marked reported
    instead, and the answer is `{"marked": n, "unreported_left": m}`. Needs only the index."""
    settings, _ = _settings(mail_dir)
    if mark_reported is not None:
        for pid in mark_reported:
            _check_number(pid, "a problem id")
        with Store.open(mail_dir) as store:
            marked = store.mark_reported(mark_reported, _now(now))
            left = group_problems(store.problems(unreported_only=True), settings)
        return {"marked": marked, "unreported_left": len(left)}
    _check_page(page)
    with Store.open(mail_dir) as store:
        entries = group_problems(store.problems(unreported_only=True), settings)
    shown, next_page = _paged(entries, page)
    return {"problems": shown, "total": len(entries), "next_page": next_page}


# -- the digest ------------------------------------------------------------------------------


def digest_items(mail_dir: Path, *, page: int = 1, up_to: int | None = None,
                 now: datetime | None = None) -> dict:
    """The mail waiting for the digest, one page of it grouped by mailbox:
    `{"since", "up_to", "groups": [{"mailbox", "count", "items"}], "total", "next_page",
    "pushed_count", "problems", "problems_total", "stale", "last_poll", "rules",
    "config_error"}`.

    `since` is when the last digest was sent (None: never); `pushed_count` the pushes since;
    `problems` every open problem, reported before or not, and the recoveries not reported yet.
    The set listed is fixed by `up_to`: the first page returns it, and later pages pass it back
    (and `digest_done` too). `count` is the number of items of that mailbox on this page.
    `stale` and `last_poll` are as in `status`, as of this call."""
    _check_page(page)
    if up_to is not None:
        _check_number(up_to, "up_to", minimum=0)
    now = _now(now)
    settings, config_error = _settings(mail_dir)
    with Store.open(mail_dir) as store:
        listing = store.digest_items(page=page, per_page=PER_PAGE, up_to=up_to)
        since = store.last_digest()
        pushed = store.push_count(since)
        raw = store.problems(unreported_only=False)
        last_poll, stale, _ = _staleness(store, now)
    groups: dict[str, list[dict]] = {}
    for message in listing["items"]:
        groups.setdefault(message["address"], []).append(
            _item(message, settings, DIGEST_EXCERPT_CHARS))
    shown, total = _problem_list(raw, settings)
    return {
        "since": since, "up_to": listing["up_to"],
        "groups": [{"mailbox": _name(settings, address), "count": len(items), "items": items}
                   for address, items in groups.items()],
        "total": listing["total"], "next_page": listing["next_page"], "pushed_count": pushed,
        "problems": shown, "problems_total": total, "stale": stale, "last_poll": last_poll,
        "rules": _rules(settings), "config_error": config_error,
    }


def digest_done(mail_dir: Path, up_to: int, *, now: datetime | None = None) -> dict:
    """The digest was sent: the messages `digest_items` listed (`up_to` is its number; 0 when it
    listed nothing) become digested and `last_digest` moves to now. `{"digested": n,
    "last_digest"}`."""
    _check_number(up_to, "up_to", minimum=0)
    with Store.open(mail_dir) as store:
        moved = store.mark_digested(_now(now), up_to)
        return {"digested": moved, "last_digest": store.last_digest()}


# -- status ----------------------------------------------------------------------------------


def _staleness(store: Store, now: datetime) -> tuple[str | None, bool, int]:
    """(time of the last poll, whether it was too long ago, the limit in minutes).

    Too long is more than two median spacings of the last polls plus 10 minutes: a poll every 5
    minutes is late after 20, a daily one after 2 days and 10 minutes. With fewer than two polls
    the spacing is taken as 5 minutes. No poll at all is stale: the check never ran."""
    times = [datetime.fromisoformat(t) for t in store.poll_times(POLLS_SAMPLED)]  # newest first
    gaps = [max((later - earlier).total_seconds() / 60, 0.0)
            for later, earlier in itertools.pairwise(times)]
    spacing = statistics.median(gaps) if gaps else DEFAULT_INTERVAL_MINUTES
    threshold = math.ceil(2 * spacing + STALE_MARGIN_MINUTES)
    if not times:
        return None, True, threshold
    elapsed = (now - times[0]).total_seconds() / 60
    return times[0].isoformat(timespec="seconds"), elapsed > threshold, threshold


def status(mail_dir: Path, *, now: datetime | None = None) -> dict:
    """`{"last_poll", "stale", "threshold_minutes", "mailboxes": [{"name", "address",
    "last_ok", "problem"}], "config_error"}`. `problem` is the mailbox's open problem
    (`{"kind", "message", "first_seen"}`) or None; `last_ok` when the mailbox last answered a
    poll. Without a readable config.toml `mailboxes` is empty and `config_error` says why."""
    now = _now(now)
    settings, config_error = _settings(mail_dir)
    with Store.open(mail_dir) as store:
        last_poll, stale, threshold = _staleness(store, now)
        open_ones: dict[str, dict] = {}
        for p in store.problems(unreported_only=False):
            if p["state"] == "open":
                open_ones.setdefault(p["address"], p)
        boxes = []
        listed: list[str] = []
        for mb in settings.mailboxes.values() if settings else ():
            if any(accounts.same_address(mb.address, other) for other in listed):
                continue
            listed.append(mb.address)
            p = open_ones.get(mb.address)
            boxes.append({
                "name": mb.name, "address": mb.address, "last_ok": store.last_ok(mb.address),
                "problem": None if p is None else {
                    "kind": p["kind"], "message": p["message"], "first_seen": p["first_seen"]},
            })
    return {"last_poll": last_poll, "stale": stale, "threshold_minutes": threshold,
            "mailboxes": boxes, "config_error": config_error}

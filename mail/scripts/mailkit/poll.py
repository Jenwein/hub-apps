"""The background check: take new mail from every mailbox into the index, wake `triage`.

`mk poll --wake` runs every few minutes as a muted cc-hub script task; `mk bg poll` is the same
without the wake, the one the restricted triage and digest tasks may run (`--wake` is outside
`bg`, so a restricted run cannot wake triage). For each mailbox in config.toml, in config
order, it reads INBOX without changing anything on the server
(EXAMINE and BODY.PEEK in the adapter: no \\Seen, no move, no delete) and:

- **New mail** (UIDs above the stored `last_uid`) is fetched a few messages at a time, parsed
  and routed by the mailbox's effective rules: a muted sender (an exact address, or `@domain`
  for the sender's domain; case does not matter), a paused mailbox, or delivery `digest` sends
  it straight to the digest (`digest`); anything else waits for the agent's verdict
  (`pending`). `last_uid` moves after each stored batch, and storing is idempotent, so a poll
  that dies half way neither loses nor duplicates mail. A UID the server lists but leaves out
  of its FETCH reply stops the new mail just below it for this poll; left out again at the
  next poll, it is stored as an unreadable message rather than skipped in silence.
- **No baseline yet** (a poll that raced `account add`, an index rebuilt on another machine)
  or **a new UIDVALIDITY** (the server renumbered INBOX): the baseline is set as `account add`
  sets it, and nothing in INBOX counts as new.
- **Backfill**: mail of the last 90 days that was in INBOX before the baseline is indexed as
  `old` (searchable, never pushed or digested), downward from `backfill_before`, at most
  `backfill_chunk` messages per poll, so a mailbox with thousands of messages fills in over
  a few polls. An old message the server leaves out is not retried: it is only missing from
  the local search.

A mailbox that fails becomes a problem in the index (`login`: the login was refused or there is
no stored password; `connect`: the server cannot be reached or dropped the connection;
`error`: anything unexpected) and the poll goes on with the next one; a mailbox that answers
ends its problems (each a recovery, told once with the next message). A config.toml that cannot
be read is one problem of address and kind `config`, and no mailbox is polled. When it can be
read, the problems of an address it no longer names are dropped: that mailbox was removed.

After the mailboxes, a run of the poll task (`background`: `mk poll --wake`, the cc-hub script
task) is recorded; `bg status` and the digest judge whether the background check still runs by
these records alone, so a poll by hand or the digest's own `mk bg poll` never makes a stopped
task look alive. With `wake` the on-demand `mail/triage` task is woken when the batch of
pending mail needs it (never woken, or woken 10 minutes ago and still pending; once an hour
after six wakes in a row that triage did not answer with a verdict, until mail is judged) or
when an open problem needs telling (`Store.problems_need_wake`: not reported, not told for that
mailbox and kind in the last day, and no wake for problems in the last 10 minutes). A recovery
never wakes triage. Whether the wakes get through (the last one, the error of a failed one, the
unanswered ones) is kept in the index for `bg status`, since what this task prints goes nowhere.
Two polls never run at once: the second finds the poll lock held and returns
`{"skipped": "another poll is running"}` without touching anything; `account remove` waits for
the lock, so that no poll still holding the old config.toml writes for a removed mailbox.
"""

from __future__ import annotations

import contextlib
import shutil
import sqlite3
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from . import accounts, config, server
from .config import Mailbox, MailError, Settings
from .message import Parsed, addressed, parse
from .server import LoginError, MailServer, ServerError
from .store import STATE_DIR, Store

FETCH_BATCH = 20  # messages per fetch: what a poll holds in memory at once
BACKFILL_CHUNK = 200  # old messages indexed per mailbox per poll
RECENT_DAYS = accounts.RECENT_DAYS
WAKE_COMMAND = ("cc-hub", "cron", "wake", "mail/triage")
# seconds; longer than cc-hub waits for one answer of cc-connect (60), so that when cc-connect
# hangs, cc-hub's own error is what the poll hears
WAKE_TIMEOUT = 90
LOCK_FILE = "poll.lock"  # in mail/.state/
CONFIG = "config"  # the address and the kind of a problem with config.toml
UNREADABLE = "(unreadable message)"  # the subject of a message nothing could be read from
SKIPPED = {"skipped": "another poll is running"}
MESSAGE_LIMIT = 1000  # characters of an error message kept in a problem


class _Failed(Exception):
    """A mailbox could not be polled: `kind` is the problem kind, the message what failed."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


# -- routing ---------------------------------------------------------------------------------


def _lower(text: str) -> str:
    return text.strip().lower()


def is_muted(sender: str, muted_senders: list[str]) -> bool:
    """Whether mail from `sender` is muted: an entry is the exact address or `@domain`, the
    whole domain of the sender (not its subdomains). Case does not matter."""
    sender = _lower(sender)
    if "@" not in sender:
        return False
    domain = "@" + sender.rpartition("@")[2]
    return any(_lower(entry) in (sender, domain) for entry in muted_senders)


def route(parsed: Parsed, rules: dict) -> str:
    """Where a new message goes by the mailbox's effective rules: "digest" (muted sender,
    paused mailbox, or delivery "digest") or "pending" (the agent judges it)."""
    if (
        rules["paused"]
        or rules["delivery"] == "digest"
        or is_muted(parsed.from_addr, rules["muted_senders"])
    ):
        return "digest"
    return "pending"


def _readable(raw: bytes) -> Parsed:
    """The parsed message; one with neither subject nor text gets a subject saying so."""
    parsed = parse(raw)
    if not parsed.subject and not parsed.text:
        parsed = replace(parsed, subject=UNREADABLE)
    return parsed


# -- the lock --------------------------------------------------------------------------------


def _busy(error: sqlite3.OperationalError) -> bool:
    text = str(error).lower()
    return "locked" in text or "busy" in text


def _acquire(path: Path, wait: float) -> sqlite3.Connection | None:
    """A connection that holds the lock in `path`, or None when another one holds it for longer
    than `wait` seconds. A damaged lock file (it holds no data, only the lock) is replaced
    once."""
    failure: sqlite3.Error | None = None
    for attempt in (1, 2):
        try:
            db = sqlite3.connect(path, timeout=wait, isolation_level=None)
        except sqlite3.Error as error:
            raise MailError(f"cannot open the poll lock {path}: {error}") from None
        try:
            db.execute("BEGIN EXCLUSIVE")
            return db
        except sqlite3.OperationalError as error:
            db.close()
            if _busy(error):
                return None
            failure = error
        except sqlite3.DatabaseError as error:  # "file is not a database"
            db.close()
            failure = error
        if attempt == 1:
            with contextlib.suppress(OSError):
                path.unlink()
    raise MailError(f"cannot use the poll lock {path} ({failure}); delete that file")


@contextlib.contextmanager
def poll_lock(mail_dir: Path, *, wait: float = 0) -> Iterator[bool]:
    """Hold the poll lock for the `with` block: yields True when this process holds it, False
    when another poll does (and, with `wait`, still did after that many seconds).

    The lock is an exclusive transaction on its own small SQLite file, `mail/.state/poll.lock`,
    not on the index: holding the index would stop every other `mk` command for the length of
    a poll. The operating system frees it when its process ends, so a poll that crashed or was
    killed leaves no stale lock behind. A poll does not wait (another one is doing its work);
    `account remove` waits for the poll to finish."""
    folder = Path(mail_dir) / STATE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    db = _acquire(folder / LOCK_FILE, wait)
    if db is None:
        yield False
        return
    try:
        yield True
    finally:
        with contextlib.suppress(sqlite3.Error):
            db.execute("ROLLBACK")
        db.close()


# -- one mailbox -----------------------------------------------------------------------------


def _scrub(text: str, password: str | None) -> str:
    if password:
        text = text.replace(password, "[password]")
    return text[:MESSAGE_LIMIT]


def _baseline(store: Store, srv: MailServer, address: str, uidvalidity: int, uidnext: int,
              since: date) -> tuple[int, int | None]:
    """Set the baseline as `account add` does: everything in INBOX now is old, and the backfill
    starts just above it when INBOX holds mail from the last 90 days. Returns
    (last_uid, backfill_before)."""
    last_uid = max(uidnext - 1, 0)
    recent = any(uid <= last_uid for uid in srv.uids_since(since))
    before = last_uid + 1 if recent else None
    store.set_baseline(address, uidvalidity, last_uid, before)
    if store.missed(address) is not None:  # a UID of the old numbering means nothing now
        store.set_missed(address, None)
    return last_uid, before


def _batches(uids: list[int]) -> Iterator[list[int]]:
    for start in range(0, len(uids), FETCH_BATCH):
        yield uids[start:start + FETCH_BATCH]


def _take_new(store: Store, srv: MailServer, address: str, uidvalidity: int, last_uid: int,
              before: int | None, rules: dict, now: datetime, entry: dict) -> int:
    """Store the mail above `last_uid`, routed; returns the new `last_uid`.

    A UID the server lists but leaves out of its FETCH reply (a reply the adapter could not
    read, or mail deleted between the search and the fetch) stops the new mail here:
    `last_uid` stays below it, and it is remembered. At the next poll, mail deleted meanwhile
    is no longer listed; a UID that is listed and left out again is stored as an unreadable
    message, so that it is never skipped in silence, and the poll goes on past it."""
    missed = store.missed(address)
    gap = None
    for batch in _batches(sorted(u for u in srv.uids_after(last_uid) if u > last_uid)):
        got = dict(srv.fetch(batch))
        for uid in batch:
            raw = got.get(uid)
            if raw is None and uid != missed:
                gap = uid
                break
            parsed = _readable(raw) if raw is not None else Parsed(subject=UNREADABLE)
            store.add(address, uid, uidvalidity, parsed, route(parsed, rules),
                      addressed(parsed, address), now=now)
            entry["new"] += 1
        last_uid = batch[-1] if gap is None else gap - 1
        store.set_baseline(address, uidvalidity, last_uid, before)
        if gap is not None:
            break
    if gap != missed:
        store.set_missed(address, gap)
    return last_uid


def _sync(store: Store, srv: MailServer, mb: Mailbox, rules: dict, now: datetime,
          backfill_chunk: int, entry: dict) -> None:
    """Take the new mail of `mb` into the index, then one chunk of its backfill. `entry["new"]`
    counts the new messages as they are stored."""
    address = mb.address
    uidvalidity, uidnext = srv.inbox_status()
    since = now.astimezone(UTC).date() - timedelta(days=RECENT_DAYS)
    state = store.state(address)
    if state is None or state["uidvalidity"] != uidvalidity:
        last_uid, before = _baseline(store, srv, address, uidvalidity, uidnext, since)
    else:
        before = state["backfill_before"]
        last_uid = _take_new(store, srv, address, uidvalidity, state["last_uid"], before,
                             rules, now, entry)
    if before is None or backfill_chunk <= 0:
        return
    older = sorted((u for u in srv.uids_since(since) if u < before), reverse=True)
    taken = older[:backfill_chunk]
    if not taken:
        store.set_baseline(address, uidvalidity, last_uid, None)
        return
    done = 0
    for batch in _batches(taken):  # the newest first
        for uid, raw in srv.fetch(batch):
            parsed = _readable(raw)
            store.add(address, uid, uidvalidity, parsed, "old", addressed(parsed, address),
                      now=now)
        done += len(batch)
        before = batch[-1] if done < len(older) else None
        store.set_baseline(address, uidvalidity, last_uid, before)


def _take(store: Store, hub: Path, settings: Settings, mb: Mailbox, connect, now: datetime,
          backfill_chunk: int, entry: dict) -> None:
    """Log in to `mb` and sync it. A failure of the mailbox is `_Failed`; a MailError that
    gets out is the index's (busy for longer than it waits)."""
    try:
        password = accounts.read_password(hub, mb.address)
    except MailError as err:
        raise _Failed("login", str(err)) from None
    try:
        srv = connect(mb, password)
    except Exception as err:  # noqa: BLE001 - only a bad host:port gets here
        raise _Failed("connect", _scrub(str(err), password)) from None
    try:
        _sync(store, srv, mb, config.effective(settings, mb.name), now, backfill_chunk, entry)
    except LoginError as err:
        raise _Failed("connect" if err.step == "connect" else "login",
                      _scrub(str(err), password)) from None
    except ServerError as err:
        raise _Failed("connect", _scrub(str(err), password)) from None
    except MailError:
        raise
    except Exception as err:  # one mailbox must not stop the others
        raise _Failed("error", _scrub(f"{type(err).__name__}: {err}", password)) from err
    finally:
        with contextlib.suppress(Exception):
            srv.close()


def _poll_mailbox(store: Store, hub: Path, settings: Settings, mb: Mailbox, connect,
                  now: datetime, backfill_chunk: int) -> dict:
    """Poll one mailbox: `{"name", "new", "error"}`. A failure is recorded as a problem of the
    mailbox; success ends its problems."""
    entry = {"name": mb.name, "new": 0, "error": None}
    try:
        _take(store, hub, settings, mb, connect, now, backfill_chunk, entry)
        store.resolve(mb.address, now)
    except _Failed as failed:
        entry["error"] = str(failed)
        with contextlib.suppress(MailError):  # a busy index: the next poll records it
            store.problem(mb.address, failed.kind, str(failed), now)
    except MailError as err:
        # the index was busy: not a problem of the mailbox, and the next poll goes on from
        # where this one stopped
        entry["error"] = str(err)
    return entry


# -- the poll --------------------------------------------------------------------------------


def _now(now: datetime | None) -> datetime:
    now = now or datetime.now(UTC)
    return now if now.tzinfo else now.replace(tzinfo=UTC)


def _wake(store: Store, wake: Callable[[], str | None] | None,
          now: datetime) -> tuple[bool, str | None]:
    """Wake triage when there is something for it: (woke, error). The outcome is kept in the
    index for `bg status`: the time of a wake that worked, the error of one that failed, and
    whether the last wake of the batch went unanswered."""
    if wake is None:
        return False, None
    store.count_unacknowledged(now)
    if not (store.batch_needs_wake(now) or store.problems_need_wake(now)):
        return False, None
    try:
        error = wake()
    except Exception as err:  # noqa: BLE001 - a failed wake is reported, not raised
        error = f"{type(err).__name__}: {err}"
    if error is not None:
        store.wake_failed(now, str(error))
        return False, str(error)
    store.mark_woken(now)
    store.mark_problems_woken(now)  # triage lists the unreported problems, whatever woke it
    return True, None


def poll(hub: Path, mail_dir: Path, *, connect=None,
         wake: Callable[[], str | None] | None = None, now: datetime | None = None,
         backfill_chunk: int = BACKFILL_CHUNK, background: bool = False) -> dict:
    """Poll every mailbox once; see the module docstring.

    `connect` is the server factory (default: `server.connect`); `wake` wakes triage and
    returns None, or an error text (default: no wake); `background` says this is a run of the
    poll task, the only kind of poll that is recorded for `bg status`. Returns
    `{"mailboxes": [{"name", "new", "error"}], "pending", "woke", "wake_error",
    "config_error"}`: `new` counts the new messages stored for that mailbox, `error` says why
    it could not be polled (None: it was); `pending` is how many messages wait for a verdict
    now; `woke` whether triage was woken, `wake_error` why waking failed; `config_error` why
    config.toml could not be read (None: it was). `{"skipped": "another poll is running"}` when
    another poll holds the lock.
    """
    hub, mail_dir, now = Path(hub), Path(mail_dir), _now(now)
    connect = connect or server.connect
    with poll_lock(mail_dir) as held:
        if not held:
            return dict(SKIPPED)
        with Store.open(mail_dir) as store:
            mailboxes: list[dict] = []
            config_error = None
            try:
                settings = config.load(mail_dir)
            except MailError as err:
                config_error = str(err)
                store.problem(CONFIG, CONFIG, config_error, now)
            else:
                store.resolve(CONFIG, now)
                # a mailbox that left config.toml has no problem left to tell
                store.forget_problems(
                    [CONFIG, *(mb.address for mb in settings.mailboxes.values())])
                polled: list[str] = []
                for mb in settings.mailboxes.values():
                    # two sections for one address (a hand edit): one inbox, polled once
                    if any(accounts.same_address(mb.address, other) for other in polled):
                        continue
                    polled.append(mb.address)
                    mailboxes.append(
                        _poll_mailbox(store, hub, settings, mb, connect, now, backfill_chunk))
            if background:
                store.record_poll(now)
            batch = store.open_batch(now)
            woke, wake_error = _wake(store, wake, now)
            return {
                "mailboxes": mailboxes,
                "pending": batch["pending"] if batch else 0,
                "woke": woke,
                "wake_error": wake_error,
                "config_error": config_error,
            }


def wake_triage(hub: Path | None = None) -> str | None:
    """Run `cc-hub cron wake mail/triage` (in `hub`, default: the working directory): None
    when cc-hub took it, else a short error text."""
    exe = shutil.which(WAKE_COMMAND[0])
    if exe is None:
        return "cc-hub not found"
    command = " ".join(WAKE_COMMAND)
    try:
        done = subprocess.run(
            [exe, *WAKE_COMMAND[1:]], cwd=hub, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=WAKE_TIMEOUT, stdin=subprocess.DEVNULL,
            check=False)
    except subprocess.TimeoutExpired:
        return f"{command} did not finish within {WAKE_TIMEOUT} seconds"
    except OSError as err:
        return f"cannot run {command}: {err}"
    if done.returncode == 0:
        return None
    detail = (done.stderr or "").strip() or (done.stdout or "").strip()
    return (detail or f"{command} failed (exit {done.returncode})")[:MESSAGE_LIMIT]

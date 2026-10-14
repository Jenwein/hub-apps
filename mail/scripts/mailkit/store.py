"""The local index of the mail app: SQLite at `mail/.state/mail.db`.

The index holds what `mk` needs between runs: per mailbox where the last poll stopped, every
message that was fetched (envelope and plain text, enough to search and to judge it), the
verdicts and pushes, the times of the polls, the problems of the mailboxes and when `triage` was
woken to tell them, a UID the server left out of a fetch, and the one batch of pending mail the
background `triage` task is woken for. Nothing in it is the only copy of a mail: the server has
the mail, and a new machine rebuilds the index by polling.

Searching is SQL `LIKE` on the envelope and the text, no full-text index. Chinese has no word
breaks and a personal mailbox is small, so a scan answers in well under a second.

One `Store` is one connection, meant for one command. The poll in the background and the agent's
own `mk` calls can run at the same time: the file is in WAL mode, a writer waits for another
writer up to `busy_timeout_ms`, and every statement group runs in one explicit transaction.

Times go in as aware `datetime`s (a naive one is read as UTC) and are kept and returned as
UTC ISO-8601 strings with whole seconds, `2026-10-14T01:30:00+00:00`: they sort and compare as
text. A mailbox is identified by its address, because the short name can change on rename.

Everything returned is plain dicts, lists, strings, numbers and None, ready for `json.dumps`.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Self

from .config import MailError
from .message import Parsed

SCHEMA_VERSION = 1
STATE_DIR = ".state"
DB_FILE = "mail.db"
BUSY_TIMEOUT_MS = 5000

# what a message can be: `old` (indexed history, never pushed or digested), `pending` (waiting
# for a verdict), `important` (judged: pushed), `digest` (judged or routed: waits for the next
# daily digest), `digested` (listed in a digest)
STATUSES = ("old", "pending", "important", "digest", "digested")
ADDRESSED = ("to", "cc", "none")
DECISIONS = ("important", "digest")
POLLS_KEPT = 200  # the status check needs the last few; older rows are dropped
PROBLEM_WAKE_MINUTES = 10  # at least this long between two wakes of triage for problems
PROBLEM_QUIET_HOURS = 24  # a mailbox's problem of one kind is told at most once in this time

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS mailboxes (
        address TEXT PRIMARY KEY,
        uidvalidity INTEGER NOT NULL,
        last_uid INTEGER NOT NULL,
        backfill_before INTEGER,
        last_ok TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY,
        address TEXT NOT NULL,
        uid INTEGER NOT NULL,
        uidvalidity INTEGER NOT NULL,
        message_id TEXT,
        in_reply_to TEXT,
        refs TEXT NOT NULL,
        date TEXT,
        date_original TEXT,
        from_addr TEXT NOT NULL,
        from_name TEXT NOT NULL,
        to_addrs TEXT NOT NULL,
        cc_addrs TEXT NOT NULL,
        subject TEXT NOT NULL,
        text TEXT NOT NULL,
        attachments TEXT NOT NULL,
        bulk INTEGER NOT NULL,
        addressed TEXT NOT NULL CHECK (addressed IN ('to', 'cc', 'none')),
        status TEXT NOT NULL
            CHECK (status IN ('old', 'pending', 'important', 'digest', 'digested')),
        added_at TEXT NOT NULL,
        judged_at TEXT,
        pushed_at TEXT,
        digested_at TEXT,
        digest_seq INTEGER,
        UNIQUE (address, uidvalidity, uid)
    )""",
    "CREATE INDEX IF NOT EXISTS messages_status ON messages (status, address, id)",
    """CREATE TABLE IF NOT EXISTS pushes (
        id INTEGER PRIMARY KEY,
        message INTEGER NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
        pushed_at TEXT NOT NULL
    )""",
    "CREATE TABLE IF NOT EXISTS polls (id INTEGER PRIMARY KEY, polled_at TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS problems (
        id INTEGER PRIMARY KEY,
        address TEXT NOT NULL,
        kind TEXT NOT NULL,
        message TEXT NOT NULL,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        reported_at TEXT,
        resolved_at TEXT,
        recovered_reported_at TEXT
    )""",
    # a mailbox has at most one open problem of each kind
    """CREATE UNIQUE INDEX IF NOT EXISTS problems_open ON problems (address, kind)
        WHERE resolved_at IS NULL""",
    # the batch of pending mail: at most one row, there only while mail is pending
    """CREATE TABLE IF NOT EXISTS batch (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        opened_at TEXT NOT NULL,
        woken_at TEXT
    )""",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)

_MESSAGE_COLUMNS = (
    "id, address, uid, uidvalidity, message_id, in_reply_to, refs, date, date_original, "
    "from_addr, from_name, to_addrs, cc_addrs, subject, text, attachments, bulk, addressed, "
    "status, added_at, judged_at, pushed_at, digested_at"
)
_SUMMARY_COLUMNS = "id, address, date, from_addr, from_name, subject, status, attachments"
_PROBLEM_COLUMNS = (
    "id, address, kind, message, first_seen, last_seen, reported_at, resolved_at"
)
# when a message happened: its Date header, else the moment it arrived
_WHEN = "COALESCE(date, added_at)"


# -- values ----------------------------------------------------------------------------------


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime | str) -> str:
    return _utc(value).isoformat(timespec="seconds")


def _header_date(value: str | None) -> str | None:
    """The UTC time of a `Parsed.date`, or None when there is none or it is not a date."""
    if not value:
        return None
    try:
        return _iso(value)
    except (ValueError, OverflowError, TypeError):
        return None


def _since(value: date | datetime | str) -> str:
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value)
        except ValueError:
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                raise MailError(
                    f"since must be a date like 2026-10-01, not {value!r}") from None
    if isinstance(value, datetime):  # a datetime is also a date: ask first
        return _iso(value)
    return datetime(value.year, value.month, value.day, tzinfo=UTC).isoformat(timespec="seconds")


def _clean(value: str) -> str:
    """`value` as text SQLite can store: a lone surrogate (from undecodable bytes) becomes '?'."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return value.encode("utf-8", "replace").decode("utf-8")
    return value


def _json(value) -> str:
    return _clean(json.dumps(value, ensure_ascii=False))


def _like(text: str) -> str:
    """`text` as a LIKE pattern that matches it anywhere, with `%`, `_` and `\\` literal."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _message(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "address": row["address"], "uid": row["uid"],
        "uidvalidity": row["uidvalidity"], "message_id": row["message_id"],
        "in_reply_to": row["in_reply_to"], "references": json.loads(row["refs"]),
        "date": row["date"], "date_original": row["date_original"],
        "from_addr": row["from_addr"], "from_name": row["from_name"],
        "to": json.loads(row["to_addrs"]), "cc": json.loads(row["cc_addrs"]),
        "subject": row["subject"], "text": row["text"],
        "attachments": json.loads(row["attachments"]), "bulk": bool(row["bulk"]),
        "addressed": row["addressed"], "status": row["status"], "added_at": row["added_at"],
        "judged_at": row["judged_at"], "pushed_at": row["pushed_at"],
        "digested_at": row["digested_at"],
    }


def _summary(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "address": row["address"], "date": row["date"],
        "from_addr": row["from_addr"], "from_name": row["from_name"],
        "subject": row["subject"], "status": row["status"],
        "attachments": json.loads(row["attachments"]),
    }


def _problem(row: sqlite3.Row) -> dict:
    entry = dict(row)
    entry["state"] = "open" if row["resolved_at"] is None else "recovered"
    return entry


def _check_paging(page: int, per_page: int) -> None:
    if page < 1 or per_page < 1:
        raise MailError("page and per_page must be at least 1")


# -- the store -------------------------------------------------------------------------------


class Store:
    @classmethod
    def open(cls, mail_dir: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> Store:
        """The index in `<mail_dir>/.state/mail.db`; the directory and the schema are created."""
        folder = Path(mail_dir) / STATE_DIR
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / DB_FILE
        db = sqlite3.connect(path, isolation_level=None, timeout=busy_timeout_ms / 1000)
        db.row_factory = sqlite3.Row
        try:
            db.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            db.execute("PRAGMA foreign_keys = ON")
            cls._wal(db)
            cls._create(db, path)
        except sqlite3.DatabaseError as error:
            db.close()
            if isinstance(error, sqlite3.OperationalError) and _is_busy(error):
                raise MailError(_BUSY) from error
            raise MailError(
                f"the mail index {path} cannot be used ({error}); it can be rebuilt: "
                "delete mail.db and its mail.db-wal and mail.db-shm files next to it"
            ) from error
        except MailError:
            db.close()
            raise
        return cls(db, path)

    @staticmethod
    def _wal(db: sqlite3.Connection) -> None:
        """Readers do not wait for the writer. Where WAL is not available (some network file
        systems) the file stays in its default mode and a reader waits for a commit."""
        try:
            mode = db.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() == "wal":
                db.execute("PRAGMA synchronous = NORMAL")  # the index can be rebuilt
        except sqlite3.OperationalError:
            pass

    @staticmethod
    def _create(db: sqlite3.Connection, path: Path) -> None:
        def version() -> int:
            return db.execute("PRAGMA user_version").fetchone()[0]

        def check(found: int) -> bool:
            if found > SCHEMA_VERSION:
                raise MailError(
                    f"the mail index {path} was made by a newer version of this app "
                    f"(index version {found}, this app reads {SCHEMA_VERSION}); update the app")
            return found == SCHEMA_VERSION

        if check(version()):
            return
        db.execute("BEGIN IMMEDIATE")
        try:
            if not check(version()):  # another command may have created it while we waited
                for statement in _SCHEMA:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise

    def __init__(self, db: sqlite3.Connection, path: Path):
        self._db = db
        self.path = path

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _transaction(self, write: bool = False):
        """A transaction: all reads see one snapshot, all writes commit together or not at all.
        A write waits for the lock from the start, so it never fails half way."""
        try:
            self._db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield self._db
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")
        except sqlite3.OperationalError as error:
            if _is_busy(error):
                raise MailError(_BUSY) from error
            raise

    # -- mailbox state -----------------------------------------------------------------------

    def set_baseline(
        self, address: str, uidvalidity: int, last_uid: int, backfill_before: int | None
    ) -> None:
        """Where the poll of `address` stands: the UIDVALIDITY of its inbox, the highest UID
        that has been taken in, and the UID below which older mail is still to be indexed
        (None: nothing left). Replaces the earlier values."""
        with self._transaction(write=True) as db:
            db.execute(
                "INSERT INTO mailboxes (address, uidvalidity, last_uid, backfill_before) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (address) DO UPDATE SET "
                "uidvalidity = excluded.uidvalidity, last_uid = excluded.last_uid, "
                "backfill_before = excluded.backfill_before",
                (address, uidvalidity, last_uid, backfill_before))

    def state(self, address: str) -> dict | None:
        """`{"uidvalidity", "last_uid", "backfill_before"}`, or None before the first baseline
        (a new mailbox, or an index rebuilt on a new machine)."""
        with self._transaction() as db:
            row = db.execute(
                "SELECT uidvalidity, last_uid, backfill_before FROM mailboxes WHERE address = ?",
                (address,)).fetchone()
        return None if row is None else dict(row)

    def forget(self, address: str) -> None:
        """Drop everything the index holds about `address`: state, messages, pushes, problems
        and when triage was woken for them, a missed UID."""
        with self._transaction(write=True) as db:
            db.execute("DELETE FROM messages WHERE address = ?", (address,))  # pushes cascade
            db.execute("DELETE FROM problems WHERE address = ?", (address,))
            db.execute("DELETE FROM mailboxes WHERE address = ?", (address,))
            self._put_meta(db, _missed_key(address), None)
            wakes = self._problem_wakes(db)
            kept = {key: when for key, when in wakes.items() if key[0] != address}
            if kept != wakes:
                self._put_problem_wakes(db, kept)
            self._sync_batch(db)

    def missed(self, address: str) -> int | None:
        """The UID of INBOX mail of `address` that the server listed but left out of its FETCH
        reply at the last poll, or None."""
        with self._transaction() as db:
            value = self._meta(db, _missed_key(address))
        return None if value is None else int(value)

    def set_missed(self, address: str, uid: int | None) -> None:
        """Remember the UID the last FETCH of `address` left out (None: there is none)."""
        with self._transaction(write=True) as db:
            self._put_meta(db, _missed_key(address), None if uid is None else str(int(uid)))

    def last_ok(self, address: str) -> str | None:
        """When `address` last answered a poll (the time of the last `resolve`), or None."""
        with self._transaction() as db:
            row = db.execute(
                "SELECT last_ok FROM mailboxes WHERE address = ?", (address,)).fetchone()
        return None if row is None else row["last_ok"]

    # -- messages ----------------------------------------------------------------------------

    def add(
        self, address: str, uid: int, uidvalidity: int, parsed: Parsed, status: str,
        addressed: str, *, now: datetime | None = None,
    ) -> int:
        """Index one message and return its id. A message already indexed under the same
        `(address, uidvalidity, uid)` is left as it is, and its id is returned. `now` is when it
        arrived (default: the clock); it dates a message that has no readable Date header."""
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}, not {status!r}")
        if addressed not in ADDRESSED:
            raise ValueError(f"addressed must be one of {', '.join(ADDRESSED)}, not {addressed!r}")
        values = (
            address, uid, uidvalidity, parsed.message_id, parsed.in_reply_to,
            _json(parsed.references), _header_date(parsed.date), parsed.date,
            parsed.from_addr, parsed.from_name, _json(parsed.to), _json(parsed.cc),
            parsed.subject, parsed.text,
            _json([asdict(attachment) for attachment in parsed.attachments]),
            int(parsed.bulk), addressed, status, _iso(now or datetime.now(UTC)),
        )
        values = tuple(_clean(v) if isinstance(v, str) else v for v in values)
        with self._transaction(write=True) as db:
            cursor = db.execute(
                "INSERT INTO messages (address, uid, uidvalidity, message_id, in_reply_to, refs, "
                "date, date_original, from_addr, from_name, to_addrs, cc_addrs, subject, text, "
                "attachments, bulk, addressed, status, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (address, uidvalidity, uid) DO NOTHING", values)
            if cursor.rowcount == 1:
                if status == "digest":
                    self._enter_digest(db, cursor.lastrowid)
                return cursor.lastrowid
            return db.execute(
                "SELECT id FROM messages WHERE address = ? AND uidvalidity = ? AND uid = ?",
                (address, uidvalidity, uid)).fetchone()["id"]

    @staticmethod
    def _enter_digest(db: sqlite3.Connection, mid: int) -> None:
        """Number a message that has just become `digest`. The numbers only grow (they are never
        reused, not even after `forget`), so "the digest as it was when it was listed" is the
        messages numbered up to a value, whatever their ids: a pending message with a low id can
        be judged `digest` after a higher-id one was routed there."""
        db.execute(
            "INSERT INTO meta (key, value) VALUES ('digest_seq', '1') "
            "ON CONFLICT (key) DO UPDATE SET value = CAST(value AS INTEGER) + 1")
        seq = db.execute("SELECT value FROM meta WHERE key = 'digest_seq'").fetchone()[0]
        db.execute("UPDATE messages SET digest_seq = ? WHERE id = ?", (int(seq), mid))

    def message(self, id: int) -> dict:
        """The stored message with all its fields, text included."""
        with self._transaction() as db:
            row = db.execute(
                f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE id = ?", (id,)).fetchone()
        if row is None:
            raise MailError(f"no message with id {id}")
        return _message(row)

    def _paged(
        self, db: sqlite3.Connection, columns: str, where: str, params: list, order: str,
        page: int, per_page: int, convert,
    ) -> dict:
        _check_paging(page, per_page)
        total = db.execute(f"SELECT COUNT(*) FROM messages WHERE {where}", params).fetchone()[0]
        rows = db.execute(
            f"SELECT {columns} FROM messages WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, per_page, (page - 1) * per_page]).fetchall()
        return {
            "items": [convert(row) for row in rows], "total": total,
            "next_page": page + 1 if page * per_page < total else None,
        }

    # -- judging -----------------------------------------------------------------------------

    def pending(self, limit: int = 10, page: int = 1) -> dict:
        """The messages waiting for a verdict, oldest first, `limit` to a page, as
        `{"items": [full messages], "total", "next_page"}`. A judged message leaves the list, so
        a caller that judges a page asks for page 1 again."""
        with self._transaction() as db:
            return self._paged(
                db, _MESSAGE_COLUMNS, "status = 'pending'", [], "id", page, limit, _message)

    def judge(self, decisions: dict[int, str], now: datetime) -> int:
        """Give verdicts: `{message id: "important" | "digest"}`. `important` marks the message
        pushed and records a push; `digest` leaves it for the next digest. A message that is not
        pending any more (a retried run) is left alone. Returns how many messages were judged.
        An unknown id or another verdict is a `MailError` and nothing is changed."""
        for decision in decisions.values():
            if decision not in DECISIONS:
                raise MailError(
                    f"a decision is one of {', '.join(DECISIONS)}, not {decision!r}")
        when = _iso(now)
        judged = 0
        with self._transaction(write=True) as db:
            for mid in decisions:
                if db.execute("SELECT 1 FROM messages WHERE id = ?", (mid,)).fetchone() is None:
                    raise MailError(f"no message with id {mid}")
            for mid, decision in decisions.items():
                if decision == "important":
                    cursor = db.execute(
                        "UPDATE messages SET status = 'important', judged_at = ?, pushed_at = ? "
                        "WHERE id = ? AND status = 'pending'", (when, when, mid))
                    if cursor.rowcount:
                        db.execute(
                            "INSERT INTO pushes (message, pushed_at) VALUES (?, ?)", (mid, when))
                else:
                    cursor = db.execute(
                        "UPDATE messages SET status = 'digest', judged_at = ? "
                        "WHERE id = ? AND status = 'pending'", (when, mid))
                    if cursor.rowcount:
                        self._enter_digest(db, mid)
                judged += cursor.rowcount
            self._sync_batch(db)
        return judged

    # -- the batch ---------------------------------------------------------------------------

    @staticmethod
    def _sync_batch(db: sqlite3.Connection, opened: str | None = None) -> dict | None:
        """The open batch (see `open_batch`), after dropping a finished one; with `opened`, a
        missing batch is created if mail is pending."""
        pending = db.execute("SELECT COUNT(*) FROM messages WHERE status = 'pending'").fetchone()[0]
        if pending == 0:
            db.execute("DELETE FROM batch")
            return None
        row = db.execute("SELECT opened_at, woken_at FROM batch").fetchone()
        if row is None and opened is not None:
            db.execute("INSERT INTO batch (id, opened_at) VALUES (1, ?)", (opened,))
            row = db.execute("SELECT opened_at, woken_at FROM batch").fetchone()
        if row is None:
            return {"opened_at": None, "woken_at": None, "pending": pending}
        return {"opened_at": row["opened_at"], "woken_at": row["woken_at"], "pending": pending}

    def open_batch(self, now: datetime) -> dict | None:
        """The batch of pending mail, `{"opened_at", "woken_at", "pending"}`, started now if
        mail is pending and no batch is open; None when nothing is pending. A batch lasts until
        every message in it has a verdict: mail that arrives meanwhile joins it."""
        with self._transaction(write=True) as db:
            return self._sync_batch(db, _iso(now))

    def mark_woken(self, now: datetime) -> None:
        """Record that `triage` was woken for the open batch. With nothing pending there is no
        batch, and nothing is recorded: a wake for a mailbox problem does not hold back the
        wake for the next mail."""
        when = _iso(now)
        with self._transaction(write=True) as db:
            if self._sync_batch(db, when) is not None:
                db.execute("UPDATE batch SET woken_at = ?", (when,))

    def batch_needs_wake(self, now: datetime, after_minutes: float = 10) -> bool:
        """True when mail is pending and the batch has not been woken, or was woken at least
        `after_minutes` ago (the run failed or is stuck). False right after `mark_woken` and
        whenever nothing is pending."""
        with self._transaction() as db:
            pending = db.execute(
                "SELECT COUNT(*) FROM messages WHERE status = 'pending'").fetchone()[0]
            row = db.execute("SELECT woken_at FROM batch").fetchone()
        if pending == 0:
            return False
        if row is None or row["woken_at"] is None:
            return True
        return (_utc(now) - _utc(row["woken_at"])).total_seconds() >= after_minutes * 60

    # -- polls and problems ------------------------------------------------------------------

    def record_poll(self, now: datetime) -> None:
        with self._transaction(write=True) as db:
            db.execute("INSERT INTO polls (polled_at) VALUES (?)", (_iso(now),))
            db.execute(
                "DELETE FROM polls WHERE id NOT IN (SELECT id FROM polls ORDER BY id DESC LIMIT ?)",
                (POLLS_KEPT,))

    def poll_times(self, limit: int = 10) -> list[str]:
        """The times of the last `limit` polls, newest first."""
        with self._transaction() as db:
            rows = db.execute(
                "SELECT polled_at FROM polls ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [row["polled_at"] for row in rows]

    def problem(self, address: str, kind: str, message: str, now: datetime) -> int:
        """Note that `address` has a problem of `kind` (`login`, `connect`, ...). The first
        time it is a new, unreported problem; while it stays open, each call only moves
        `last_seen` and keeps the latest `message`. Returns the problem's id."""
        when = _iso(now)
        message = _clean(message)
        with self._transaction(write=True) as db:
            row = db.execute(
                "SELECT id FROM problems WHERE address = ? AND kind = ? AND resolved_at IS NULL",
                (address, kind)).fetchone()
            if row is not None:
                db.execute(
                    "UPDATE problems SET last_seen = ?, message = ? WHERE id = ?",
                    (when, message, row["id"]))
                return row["id"]
            return db.execute(
                "INSERT INTO problems (address, kind, message, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?)", (address, kind, message, when, when)).lastrowid

    def resolve(self, address: str, now: datetime) -> int:
        """`address` answered: its open problems end (each is now a recovery to report once),
        and `last_ok` moves to `now`. Returns how many problems ended."""
        when = _iso(now)
        with self._transaction(write=True) as db:
            db.execute("UPDATE mailboxes SET last_ok = ? WHERE address = ?", (when, address))
            return db.execute(
                "UPDATE problems SET resolved_at = ? WHERE address = ? AND resolved_at IS NULL",
                (when, address)).rowcount

    def problems(self, unreported_only: bool) -> list[dict]:
        """Problems to tell the user about, oldest first: `{"id", "address", "kind", "message",
        "first_seen", "last_seen", "reported_at", "resolved_at", "state"}` with `state` `open`
        or `recovered`. A recovery is listed until it is reported. With `unreported_only`, open
        problems that were reported before are left out; without it they are all listed (the
        digest repeats them). A problem that recovered before it was ever reported is one
        `recovered` entry whose `reported_at` is None."""
        extra = "AND reported_at IS NULL" if unreported_only else ""
        with self._transaction() as db:
            rows = db.execute(
                f"SELECT {_PROBLEM_COLUMNS} FROM problems WHERE "
                f"(resolved_at IS NULL {extra}) OR (resolved_at IS NOT NULL "
                "AND recovered_reported_at IS NULL) ORDER BY id").fetchall()
        return [_problem(row) for row in rows]

    # -- waking triage for problems ---------------------------------------------------------

    @staticmethod
    def _meta(db: sqlite3.Connection, key: str) -> str | None:
        row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    @staticmethod
    def _put_meta(db: sqlite3.Connection, key: str, value: str | None) -> None:
        if value is None:
            db.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))

    @classmethod
    def _problem_wakes(cls, db: sqlite3.Connection) -> dict[tuple[str, str], str]:
        """When triage was last woken for each (address, kind)."""
        return {
            (address, kind): when
            for address, kind, when in json.loads(cls._meta(db, "problem_wakes") or "[]")}

    @classmethod
    def _put_problem_wakes(cls, db: sqlite3.Connection, wakes: dict) -> None:
        cls._put_meta(db, "problem_wakes", json.dumps(
            [[address, kind, when] for (address, kind), when in sorted(wakes.items())],
            ensure_ascii=False))

    @staticmethod
    def _open_unreported(db: sqlite3.Connection) -> list[sqlite3.Row]:
        return db.execute(
            "SELECT address, kind FROM problems WHERE resolved_at IS NULL "
            "AND reported_at IS NULL ORDER BY id").fetchall()

    def problems_need_wake(
        self, now: datetime, after_minutes: float = PROBLEM_WAKE_MINUTES,
        quiet_hours: float = PROBLEM_QUIET_HOURS,
    ) -> bool:
        """Whether `triage` should be woken to tell the user about a mailbox problem.

        True when an open problem has not been reported, and its mailbox's problem of that kind
        was neither woken for nor reported in the last `quiet_hours` (so a problem is pushed
        once, a mailbox whose connection comes and goes is pushed once a day, and a problem
        that triage was woken for but did not mark is not pushed at every poll; the digest
        lists the open ones every day), and the last wake for problems was at least
        `after_minutes` ago. A recovery never wakes triage: it is told with the next message."""
        moment = _utc(now)
        with self._transaction() as db:
            rows = self._open_unreported(db)
            if not rows:
                return False
            last = self._meta(db, "problems_woken_at")
            told = self._problem_wakes(db)
            for row in db.execute(
                    "SELECT address, kind, MAX(reported_at) AS at FROM problems "
                    "WHERE reported_at IS NOT NULL GROUP BY address, kind"):
                key = (row["address"], row["kind"])
                told[key] = max(told.get(key, row["at"]), row["at"])
        if last is not None and (moment - _utc(last)).total_seconds() < after_minutes * 60:
            return False
        return any(
            (key := (row["address"], row["kind"])) not in told
            or (moment - _utc(told[key])).total_seconds() >= quiet_hours * 3600
            for row in rows)

    def mark_problems_woken(self, now: datetime) -> int:
        """`triage` was woken: the open problems not yet reported go with it (it lists them).
        Records the time for each one's mailbox and kind, and as the last wake for problems.
        Returns how many problems that is; with none, nothing is recorded."""
        when = _iso(now)
        with self._transaction(write=True) as db:
            rows = self._open_unreported(db)
            if not rows:
                return 0
            wakes = self._problem_wakes(db)
            for row in rows:
                wakes[(row["address"], row["kind"])] = when
            self._put_problem_wakes(db, wakes)
            self._put_meta(db, "problems_woken_at", when)
        return len(rows)

    def mark_reported(self, ids: list[int], now: datetime) -> int:
        """The user was told about these entries of `problems`. An open problem will not be
        listed as new again; a recovered one will not be listed again at all. Unknown ids are
        ignored. Returns how many problems were marked."""
        when = _iso(now)
        marked = 0
        with self._transaction(write=True) as db:
            for pid in ids:
                marked += db.execute(
                    "UPDATE problems SET reported_at = COALESCE(reported_at, ?), "
                    "recovered_reported_at = CASE WHEN resolved_at IS NULL "
                    "THEN recovered_reported_at ELSE COALESCE(recovered_reported_at, ?) END "
                    "WHERE id = ?", (when, when, pid)).rowcount
        return marked

    # -- queries -----------------------------------------------------------------------------

    def search(
        self, text: str, *, address: str | None = None, sender: str | None = None,
        since: date | datetime | str | None = None, page: int = 1, per_page: int = 10,
    ) -> dict:
        """Messages whose subject, sender or text contains `text` (case-insensitive for ASCII;
        any other character must match exactly), newest first, as `{"items": [summaries],
        "total", "next_page"}`. `text` is one substring, not a list of words; empty matches
        all. `address` is a mailbox, `sender` a substring of the From address or name, `since`
        a date (UTC midnight), a datetime or an ISO string: only messages from then on."""
        clauses, params = ["1 = 1"], []
        text = text.strip()
        if text:
            columns = ("subject", "from_addr", "from_name", "text")
            clauses.append("(" + " OR ".join(
                f"lower({column}) LIKE lower(?) ESCAPE '\\'" for column in columns) + ")")
            params += [_like(text)] * len(columns)
        if address:
            clauses.append("lower(address) = lower(?)")
            params.append(address)
        if sender and sender.strip():
            clauses.append(
                "(lower(from_addr) LIKE lower(?) ESCAPE '\\' "
                "OR lower(from_name) LIKE lower(?) ESCAPE '\\')")
            params += [_like(sender.strip())] * 2
        if since is not None:
            clauses.append(f"{_WHEN} >= ?")
            params.append(_since(since))
        with self._transaction() as db:
            return self._paged(
                db, _SUMMARY_COLUMNS, " AND ".join(clauses), params,
                f"{_WHEN} DESC, id DESC", page, per_page, _summary)

    def pushes(self, limit: int = 5) -> list[dict]:
        """The last `limit` pushes, newest first: the summary of the message (as in `search`)
        and `pushed_at`. What the user refers to with "reply to that one"."""
        with self._transaction() as db:
            rows = db.execute(
                "SELECT m.id, m.address, m.date, m.from_addr, m.from_name, m.subject, m.status, "
                "m.attachments, p.pushed_at FROM pushes p JOIN messages m ON m.id = p.message "
                "ORDER BY p.id DESC LIMIT ?", (limit,)).fetchall()
        return [{**_summary(row), "pushed_at": row["pushed_at"]} for row in rows]

    def push_count(self, since: datetime | str | None = None) -> int:
        """How many pushes there were, or since (after) a time."""
        with self._transaction() as db:
            if since is None:
                return db.execute("SELECT COUNT(*) FROM pushes").fetchone()[0]
            return db.execute(
                "SELECT COUNT(*) FROM pushes WHERE pushed_at > ?", (_iso(since),)).fetchone()[0]

    def digest_items(self, page: int = 1, per_page: int = 10, up_to: int | None = None) -> dict:
        """The messages waiting for the next digest as `{"items": [full messages], "total",
        "next_page", "up_to"}`, grouped by mailbox (ordered by address, then arrival).

        The listing is a fixed set. Without `up_to` it is every `digest` message now, and the
        result's `up_to` names that set; pass the same `up_to` for the later pages and to
        `mark_digested`. Mail that becomes `digest` meanwhile (a poll that routes a muted
        sender's mail, a triage that judges one) is outside it: it cannot shift the pages, and
        it is not marked digested without having been listed. `up_to` is a number from this
        method, not a message id; 0 means there was nothing."""
        with self._transaction() as db:
            if up_to is None:
                up_to = db.execute(
                    "SELECT COALESCE(MAX(digest_seq), 0) FROM messages WHERE status = 'digest'"
                ).fetchone()[0]
            listing = self._paged(
                db, _MESSAGE_COLUMNS, "status = 'digest' AND digest_seq <= ?", [up_to],
                "address, id", page, per_page, _message)
        listing["up_to"] = up_to
        return listing

    def mark_digested(self, now: datetime, up_to: int | None = None) -> int:
        """A digest was sent: the `digest` messages become `digested`, and `last_digest` is
        `now`. With `up_to` from `digest_items`, only the messages that listing covered; without
        it, every `digest` message. Returns how many messages moved."""
        when = _iso(now)
        bound, params = ("", [when]) if up_to is None else (" AND digest_seq <= ?", [when, up_to])
        with self._transaction(write=True) as db:
            moved = db.execute(
                "UPDATE messages SET status = 'digested', digested_at = ? "
                f"WHERE status = 'digest'{bound}", params).rowcount
            db.execute(
                "INSERT INTO meta (key, value) VALUES ('last_digest', ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (when,))
        return moved

    def last_digest(self) -> str | None:
        """When the last digest was sent, or None."""
        with self._transaction() as db:
            row = db.execute("SELECT value FROM meta WHERE key = 'last_digest'").fetchone()
        return None if row is None else row["value"]


_BUSY = "the mail index is busy (another mk command is writing to it); try again in a moment"


def _missed_key(address: str) -> str:
    return f"missed:{address}"


def _is_busy(error: sqlite3.OperationalError) -> bool:
    text = str(error).lower()
    return "locked" in text or "busy" in text

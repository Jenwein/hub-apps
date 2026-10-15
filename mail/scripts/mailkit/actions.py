"""What the user asks to be done to their mail: mark read or unread, archive, delete, and
download an attachment. The background commands only look; these change a mail server, so each
one is built to be safe to run once and to be refused when it cannot be sure what it would touch.

- `flag`: mark messages read or unread. `archive`: move them out of INBOX into the archive
  folder. Both can be undone, so neither asks for a confirmation.
- `delete_list`, then `delete`: a deletion is only ever of a set the user saw listed.
  `delete_list` saves the exact set (the ids, and where each message is on the server) in
  `mail/.state/delete-lists/<list id>.json` and shows the first 10; `delete <list id>` moves
  exactly those messages to the Trash folder and forgets the list. Mail is moved to Trash, never
  erased: a mailbox that has no Trash folder gets no deletion at all (never `\\Deleted` plus
  EXPUNGE as a substitute).
- `download_attachment`: fetch one attachment (BODY.PEEK, so nothing is marked read) and save it
  in `mail/attachments/<address>/<YYYY-MM>/<id>-<name>`.

Rules that hold for every command here:

- A message is found through the index by its id. Before anything is sent to a server its
  UIDVALIDITY is checked twice, against the mailbox's current one in the index and against the
  server's own: a UID of an INBOX that was renumbered may now be another message, so that
  message is refused ("search again") and left alone.
- A message that was archived or deleted is recorded in the index with its new folder
  (`Store.mark_moved`), so a second archive or delete of it is refused ("already moved to ...")
  instead of copying it again. A server that can neither MOVE nor UID EXPUNGE copies the message
  and leaves the original in INBOX flagged `\\Deleted`: the move is recorded all the same, and
  the result says in `warnings` that the original stays until a mail client expunges it.
- Ids are grouped by mailbox, one connection per mailbox. A mailbox that fails (no password, a
  refused login, a dropped connection, no Trash folder) is an entry of `errors` with its ids; the
  others still go ahead. Nothing here raises for one message or one mailbox.
- `flag`, `archive` and `delete` return `{"ok", "done", "ids", "mailboxes", "refused",
  "errors", ...}`: `ok` is true when every id was done; `refused` are `{"id", "reason"}` for
  messages that were never sent to a server; `errors` are `{"mailbox", "ids", "error"}`. A
  password is never in any of it.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import accounts, config, server
from .background import MAX_NUMBER
from .config import Mailbox, MailError, Settings
from .message import attachment_bytes
from .providers import PROVIDERS
from .server import MailServer
from .store import STATE_DIR, Store

PER_PAGE = 10  # items of a delete list shown at a time
LISTS_DIR = "delete-lists"  # in mail/.state/
ATTACHMENTS_DIR = "attachments"  # in mail/
LIST_ID_LENGTH = 8
LIST_ID_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"
NAME_CHARS = 150  # the longest attachment file name kept, in characters ...
NAME_BYTES = 200  # ... and in UTF-8 bytes (a Linux file name may have 255, id and `(2)` included)
EXTENSION_CHARS = 20  # a longer "extension" is not one
MAX_COPIES = 99  # `name (2)` ... `name (99)` for different files of one name
MESSAGE_LIMIT = 1000  # characters of an error kept in a result
TRASH = "\\Trash"
ARCHIVE = "\\Archive"
ALL_MAIL = "\\All"  # Gmail's "All Mail": archiving is moving a message out of INBOX into it
ARCHIVE_NAME = "Archive"  # the folder `--create` makes
_UNSELECTABLE = frozenset({"\\noselect", "\\nonexistent"})


# -- values ----------------------------------------------------------------------------------


def _now(now: datetime | None) -> datetime:
    now = now or datetime.now(UTC)
    return now if now.tzinfo else now.replace(tzinfo=UTC)


def _scrub(text: str, password: str | None) -> str:
    if password:
        text = text.replace(password, "[password]")
    return text[:MESSAGE_LIMIT]


def _check_id(mid: int) -> int:
    if isinstance(mid, bool) or not isinstance(mid, int) or not 0 <= mid <= MAX_NUMBER:
        raise MailError(f"no message with id {mid}")
    return mid


def _ids(ids) -> list[int]:
    """The ids, each once, in the order given."""
    ordered = list(dict.fromkeys(_check_id(mid) for mid in ids))
    if not ordered:
        raise MailError("give at least one message id")
    return ordered


def _mailbox_of(settings: Settings | None, address: str) -> Mailbox | None:
    for mb in settings.mailboxes.values() if settings else ():
        if accounts.same_address(mb.address, address):
            return mb
    return None


def _optional_settings(mail_dir: Path) -> Settings | None:
    try:
        return config.load(mail_dir)
    except MailError:
        return None


# -- which messages can be acted on ----------------------------------------------------------


def _stale(mid: int) -> str:
    return (
        f"message {mid} is out of date: the server renumbered its mailbox after the message "
        "was indexed, so where it is on the server is unknown and it was left alone; "
        "search again and use the id from the new result")


def _moved(mid: int, item: dict) -> str:
    text = (f"message {mid} was already moved to {item['moved_to']}; "
            "this works on mail that is still in INBOX")
    if item["moved_expunged"] is False:
        text += (" (the server could not remove the original, which stays flagged deleted in "
                 "INBOX until a mail client expunges it)")
    return text


def _why_not(store: Store, item: dict | None, mid: int, validity: dict) -> str | None:
    """Why message `mid` (its `Store.locate` entry, or None) cannot be acted on, or None when
    it can. `validity` caches the current UIDVALIDITY of each mailbox for the caller."""
    if item is None:
        return f"no message with id {mid}"
    if item["moved_to"] is not None:
        return _moved(mid, item)
    address = item["address"]
    if address not in validity:
        state = store.state(address)
        validity[address] = None if state is None else state["uidvalidity"]
    if validity[address] != item["uidvalidity"]:
        return _stale(mid)
    return None


@dataclass
class _Run:
    """What happened to a set of messages, mailbox by mailbox."""

    entries: list[dict] = field(default_factory=list)  # one per mailbox that went through
    done: list[int] = field(default_factory=list)
    refused: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def result(self, ids: list[int], **extra) -> dict:
        done = set(self.done)
        return {
            "ok": not self.refused and not self.errors, **extra, "done": len(done),
            "ids": [mid for mid in ids if mid in done], "mailboxes": self.entries,
            "refused": self.refused, "errors": self.errors,
        }


def _plan(store: Store, ids: list[int], run: _Run) -> dict[str, list[dict]]:
    """The messages `ids` that can be acted on, grouped by mailbox address; the others are
    refused in `run`."""
    found = store.locate(ids)
    validity: dict = {}
    groups: dict[str, list[dict]] = {}
    for mid in ids:
        item = found.get(mid)
        reason = _why_not(store, item, mid, validity)
        if reason is None:
            groups.setdefault(item["address"], []).append(item)
        else:
            run.refused.append({"id": mid, "reason": reason})
    return groups


Work = Callable[[MailServer, Mailbox, list[dict]], tuple[dict, list[str]]]


def _on_servers(hub: Path, settings: Settings, groups: dict[str, list[dict]], connect,
                work: Work, run: _Run) -> None:
    """Run `work(server, mailbox, items) -> (entry, warnings)` on each mailbox of `groups`,
    over one connection. A failure of one mailbox is an error entry for its messages and does
    not stop the others; so is a UIDVALIDITY the server no longer has."""
    for address, items in groups.items():
        mb = _mailbox_of(settings, address)
        ids = [item["id"] for item in items]
        password = srv = None
        try:
            if mb is None:
                raise MailError(f"the mailbox {address} is not in config.toml any more")
            password = accounts.read_password(hub, address)
            srv = connect(mb, password)
            if srv.inbox_status()[0] != items[0]["uidvalidity"]:
                run.refused += [{"id": mid, "reason": _stale(mid)} for mid in ids]
                continue
            entry, warnings = work(srv, mb, items)
        except Exception as err:  # noqa: BLE001 - one mailbox must not stop the others
            detail = str(err) if isinstance(err, MailError) else f"{type(err).__name__}: {err}"
            run.errors.append({
                "mailbox": mb.name if mb else address, "ids": ids,
                "error": _scrub(detail, password)})
        else:
            run.entries.append(entry)
            run.done += ids
            run.warnings += warnings
        finally:
            if srv is not None:
                with contextlib.suppress(Exception):
                    srv.close()


# -- flag ------------------------------------------------------------------------------------


def flag(hub: Path, mail_dir: Path, ids: list[int], seen: bool, *, connect=None) -> dict:
    """Mark the messages `ids` read (`seen`) or unread. `{"ok", "seen", "done", "ids",
    "mailboxes": [{"mailbox", "count"}], "refused", "errors"}`; `done` counts the messages the
    server was told about."""
    ids, mail_dir = _ids(ids), Path(mail_dir)
    settings = config.load(mail_dir)
    connect = connect or server.connect
    run = _Run()

    def work(srv, mb, items):
        srv.set_seen([item["uid"] for item in items], seen)
        return {"mailbox": mb.name, "count": len(items)}, []

    with Store.open(mail_dir) as store:
        groups = _plan(store, ids, run)
    _on_servers(Path(hub), settings, groups, connect, work, run)
    return run.result(ids, seen=seen)


# -- moving: archive and delete --------------------------------------------------------------


def _special(folders: list, attribute: str) -> str | None:
    """The first folder the server marks with SPECIAL-USE `attribute` (`\\Trash`, ...)."""
    wanted = attribute.lower()
    for name, flags in folders:
        marks = {mark.lower() for mark in flags}
        if wanted in marks and not marks & _UNSELECTABLE:
            return name
    return None


def _named(folders: list, names) -> str | None:
    """The first of `names` that is a folder (case does not matter), as the server spells it."""
    for wanted in names:
        for name, flags in folders:
            if name.casefold() == wanted.casefold() and not (
                    {mark.lower() for mark in flags} & _UNSELECTABLE):
                return name
    return None


def _archive_folder(srv: MailServer, mb: Mailbox, create: bool) -> tuple[str, dict]:
    """(folder, {"created"}) where `mb`'s mail is archived. Gmail: the folder marked `\\All`
    (All Mail: archiving is leaving INBOX); the others: the one marked `\\Archive`; else a
    name the provider table knows. With none: MailError that says to ask the user, unless
    `create`, which makes a folder called Archive (or uses one already so called that the
    server does not mark)."""
    folders = srv.folders()
    provider = PROVIDERS.get(mb.provider)
    attribute = ALL_MAIL if mb.provider == "gmail" else ARCHIVE
    found = _special(folders, attribute) or _named(folders, provider.archive if provider else ())
    if found is not None:
        return found, {"created": False}
    same_name = _named(folders, (ARCHIVE_NAME,))
    if not create:
        if same_name is not None:
            there = (f" The server has a folder named {same_name}, but does not mark it as "
                     "the archive; --create uses that folder.")
        else:
            there = ""
        raise MailError(
            f"{mb.name} has no archive folder (the server marks none as the archive).{there} "
            f"Nothing was moved; ask the user whether to {'use' if same_name else 'create'} a "
            f"folder named {ARCHIVE_NAME} for archived mail, and if so run the command again "
            "with --create.")
    if same_name is not None:
        return same_name, {"created": False}
    srv.create_folder(ARCHIVE_NAME)
    return ARCHIVE_NAME, {"created": True}


def _trash_folder(srv: MailServer, mb: Mailbox) -> tuple[str, dict]:
    """(folder, {}) where `mb`'s deleted mail goes: the folder marked `\\Trash`, else a name the
    provider table knows. MailError when there is none: nothing is deleted any other way."""
    folders = srv.folders()
    provider = PROVIDERS.get(mb.provider)
    found = _special(folders, TRASH) or _named(folders, provider.trash if provider else ())
    if found is None:
        raise MailError(
            f"{mb.name} has no Trash folder (the server marks none as the trash), so nothing "
            "was deleted: mail is only ever moved to Trash, never erased. Tell the user.")
    return found, {}


def _kept(mb: Mailbox, count: int, folder: str) -> str:
    return (
        f"{mb.name}: this server cannot remove mail from INBOX by itself, so the {count} "
        f"message(s) were copied to {folder} and the originals are only flagged deleted in "
        "INBOX; they stay visible there until a mail client expunges them")


def _mover(store: Store, when: datetime, find_folder) -> Work:
    """The work of `archive` and `delete`: move the uids to the folder `find_folder(server,
    mailbox)` names, then record in the index where each message went."""

    def work(srv, mb, items):
        folder, extra = find_folder(srv, mb)
        moved = srv.move([item["uid"] for item in items], folder)
        expunged = bool(moved.get("expunged", True))
        warnings = []
        try:
            store.mark_moved([item["id"] for item in items], folder, expunged, when)
        except MailError as err:  # the mail is moved; only the index is behind
            warnings.append(
                f"{mb.name}: the messages were moved to {folder}, but the index could not "
                f"record it ({err}); do not move them again")
        if not expunged:
            warnings.append(_kept(mb, len(items), folder))
        entry = {"mailbox": mb.name, "count": len(items), "folder": folder, **extra,
                 "expunged": expunged}
        return entry, warnings

    return work


def archive(hub: Path, mail_dir: Path, ids: list[int], *, create: bool = False,
            connect=None, now: datetime | None = None) -> dict:
    """Move the messages `ids` out of INBOX to the archive of their mailbox. `{"ok", "done",
    "ids", "mailboxes": [{"mailbox", "count", "folder", "created", "expunged"}], "refused",
    "errors", "warnings"}`. A mailbox with no archive folder is an error entry that says to ask
    the user and run again with `create`, which makes the folder Archive."""
    ids, mail_dir = _ids(ids), Path(mail_dir)
    settings = config.load(mail_dir)
    connect = connect or server.connect
    run = _Run()
    with Store.open(mail_dir) as store:
        groups = _plan(store, ids, run)
        work = _mover(store, _now(now), lambda srv, mb: _archive_folder(srv, mb, create))
        _on_servers(Path(hub), settings, groups, connect, work, run)
    return {**run.result(ids), "warnings": run.warnings}


# -- delete lists ----------------------------------------------------------------------------


def _lists_dir(mail_dir: Path) -> Path:
    return Path(mail_dir) / STATE_DIR / LISTS_DIR


def _check_list_id(list_id: str) -> str:
    if not (isinstance(list_id, str) and len(list_id) == LIST_ID_LENGTH
            and all(char in LIST_ID_CHARS for char in list_id)):
        raise MailError(
            f"{list_id!r} is not a delete list id (delete-list prints one: "
            f"{LIST_ID_LENGTH} lower-case letters and digits)")
    return list_id


def _list_path(mail_dir: Path, list_id: str) -> Path:
    return _lists_dir(mail_dir) / f"{_check_list_id(list_id)}.json"


def _damaged(list_id: str, why: str) -> MailError:
    return MailError(
        f"the delete list {list_id} cannot be read ({why}); make a new one with delete-list")


def _read_list(mail_dir: Path, list_id: str) -> dict:
    path = _list_path(mail_dir, list_id)
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        raise MailError(
            f"no delete list {list_id}: it was never made, or it was used already; make a new "
            "one with delete-list and show it to the user") from None
    except (OSError, ValueError) as err:
        raise _damaged(list_id, str(err)) from None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items or not all(
        isinstance(entry, dict)
        and all(isinstance(entry.get(key), int) and not isinstance(entry.get(key), bool)
                for key in ("id", "uid", "uidvalidity"))
        and isinstance(entry.get("address"), str)
        for entry in items
    ):
        raise _damaged(list_id, "it is not a list of messages")
    return data


def _write_list(mail_dir: Path, list_id: str, data: dict) -> None:
    path = _list_path(mail_dir, list_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        config.atomic_write(path, json.dumps(data, ensure_ascii=False).encode("utf-8"), mode=0o600)
    except OSError as err:
        raise MailError(
            f"cannot save the delete list in {path.parent}: {err.strerror or err}") from None


def _new_list_id(mail_dir: Path) -> str:
    for _ in range(100):
        list_id = "".join(secrets.choice(LIST_ID_CHARS) for _ in range(LIST_ID_LENGTH))
        if not _list_path(mail_dir, list_id).exists():
            return list_id
    raise MailError("cannot find a free delete list id")  # (36^8 of them)


def _shown(entry: dict) -> dict:
    return {key: entry[key]
            for key in ("id", "mailbox", "from", "from_name", "subject", "date")}


def _page_of(list_id: str, entries: list[dict], page: int) -> dict:
    if page < 1:
        raise MailError("page must be at least 1")
    start = (page - 1) * PER_PAGE
    return {
        "list_id": list_id, "total": len(entries),
        "items": [_shown(entry) for entry in entries[start:start + PER_PAGE]],
        "next_page": page + 1 if start + PER_PAGE < len(entries) else None,
    }


def delete_list(mail_dir: Path, ids: list[int], *, now: datetime | None = None) -> dict:
    """Save the set `ids` as a delete list and show it: `{"list_id", "total", "items": the
    first 10 as {"id", "mailbox", "from", "from_name", "subject", "date"}, "next_page"}`. The
    user is shown this and says "delete"; only then does `delete` run. The set must be exactly
    what the user sees, so a message that cannot be listed (unknown, already moved, out of
    date) is a MailError and no list is made. Nothing on a server is touched."""
    ids, mail_dir = _ids(ids), Path(mail_dir)
    settings = _optional_settings(mail_dir)
    with Store.open(mail_dir) as store:
        found = store.locate(ids)
        validity: dict = {}
        for mid in ids:
            reason = _why_not(store, found.get(mid), mid, validity)
            if reason is not None:
                raise MailError(reason)
    entries = []
    for mid in ids:
        item = found[mid]
        mb = _mailbox_of(settings, item["address"])
        entries.append({
            "id": mid, "address": item["address"], "uid": item["uid"],
            "uidvalidity": item["uidvalidity"], "mailbox": mb.name if mb else item["address"],
            "from": item["from_addr"], "from_name": item["from_name"],
            "subject": item["subject"], "date": item["date"],
        })
    list_id = _new_list_id(mail_dir)
    _write_list(mail_dir, list_id, {
        "version": 1, "created_at": _now(now).isoformat(timespec="seconds"), "items": entries})
    return _page_of(list_id, entries, 1)


def show_delete_list(mail_dir: Path, list_id: str, *, page: int = 1) -> dict:
    """A page (10 items) of a saved delete list, as `delete_list` shows its first page."""
    return _page_of(list_id, _read_list(Path(mail_dir), list_id)["items"], page)


def delete(hub: Path, mail_dir: Path, list_id: str, *, connect=None,
           now: datetime | None = None) -> dict:
    """Move exactly the messages of a delete list to the Trash folder of their mailbox, then
    forget the list. `{"ok", "list_id", "done", "ids", "mailboxes": [{"mailbox", "count",
    "folder", "expunged"}], "refused", "errors", "warnings", "remaining"}`.

    The list is checked again against the index: a message that was archived or deleted since,
    or whose mailbox was renumbered, is refused. A mailbox that fails keeps its messages in the
    list (`remaining` counts them), so that running `delete` again tries just those; a used up
    list, or one never made, is a MailError."""
    mail_dir = Path(mail_dir)
    saved = _read_list(mail_dir, list_id)
    entries = saved["items"]
    ids = list(dict.fromkeys(entry["id"] for entry in entries))
    settings = config.load(mail_dir)
    connect = connect or server.connect
    run = _Run()
    with Store.open(mail_dir) as store:
        found = store.locate(ids)
        validity: dict = {}
        groups: dict[str, list[dict]] = {}
        for entry in entries:
            mid, item = entry["id"], found.get(entry["id"])
            reason = _why_not(store, item, mid, validity)
            if reason is None and (item["address"], item["uid"], item["uidvalidity"]) != (
                    entry["address"], entry["uid"], entry["uidvalidity"]):
                reason = _stale(mid)  # (what the user saw is not what the index holds)
            if reason is None:
                groups.setdefault(item["address"], []).append(item)
            else:
                run.refused.append({"id": mid, "reason": reason})
        work = _mover(store, _now(now), _trash_folder)
        _on_servers(Path(hub), settings, groups, connect, work, run)

    failed = {mid for error in run.errors for mid in error["ids"]}
    left = [entry for entry in entries if entry["id"] in failed]
    if left:  # a try again takes just these
        run.warnings.append(
            f"{len(left)} message(s) were not moved; run delete {list_id} again to try those "
            "once more")
        try:
            _write_list(mail_dir, list_id, {**saved, "items": left})
        except MailError as err:  # the whole list stays: the messages moved are refused next time
            run.warnings.append(f"the list could not be shortened ({err})")
    else:
        with contextlib.suppress(OSError):
            _list_path(mail_dir, list_id).unlink()
    return {**run.result(ids, list_id=list_id), "warnings": run.warnings,
            "remaining": len(left)}


# -- attachments -----------------------------------------------------------------------------

# Characters that are taken out of an attachment name: control characters, the soft hyphen,
# zero-width and direction-changing characters (they hide what a name says), the byte order mark.
_REMOVED = (
    {*range(0x20), 0x7F, 0xAD, 0x34F, 0xFEFF}
    | {*range(0x200B, 0x2010), *range(0x202A, 0x202F), *range(0x2060, 0x206A)})
_SURROGATES = range(0xD800, 0xE000)  # what bytes that are not text became in a name
_ILLEGAL = {*map(ord, '/:*?"<>|'), 92}  # path separators (92: backslash), what Windows refuses
_CLEAN = {
    **dict.fromkeys(_REMOVED),  # deleted
    **dict.fromkeys(_SURROGATES, "_"),
    **dict.fromkeys(_ILLEGAL, "_"),
}
# Windows device names, which are devices with any extension too (COM1 ... and COM with a
# superscript digit, 0xB9, 0xB2 and 0xB3)
_DEVICES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"{kind}{digit}" for kind in ("com", "lpt") for digit in "123456789"}
    | {f"{kind}{chr(code)}" for kind in ("com", "lpt") for code in (0xB9, 0xB2, 0xB3)})


def safe_filename(name: str, index: int) -> str:
    """`name` as a file name that every file system takes and that cannot leave its folder.

    Control and direction-changing characters go; path separators, `:` and the other characters
    Windows refuses become `_`; leading dots and spaces, and trailing ones, go (no hidden file,
    no `..`, Windows drops a trailing dot); a Windows device name (`CON`, `aux.txt`, `COM1`) gets
    a `_` in front; the name is cut to 150 characters and 200 bytes with its extension kept.
    Chinese and other letters stay as they are. A name nothing is left of is
    `attachment-<index>`."""
    name = name.translate(_CLEAN).strip().lstrip(". ").rstrip(". ")
    if not name:
        return f"attachment-{index}"
    if name.split(".", 1)[0].rstrip().casefold() in _DEVICES:
        name = "_" + name
    return _shorten(name)


def _shorten(name: str) -> str:
    if len(name) <= NAME_CHARS and len(name.encode("utf-8")) <= NAME_BYTES:
        return name
    stem, ext = os.path.splitext(name)
    if len(ext) > EXTENSION_CHARS:
        stem, ext = name, ""
    stem = stem[:max(NAME_CHARS - len(ext), 1)]
    budget = NAME_BYTES - len(ext.encode("utf-8"))
    while len(stem) > 1 and len(stem.encode("utf-8")) > budget:
        stem = stem[:-1]
    return stem.rstrip(". ") + ext


def _month(item: dict) -> str:
    """`YYYY-MM` (UTC) of the message: its Date, else when it arrived."""
    month = (item["date"] or item["added_at"])[:7]
    return month if re.fullmatch(r"[0-9]{4}-[0-9]{2}", month) else "undated"


def _holds(path: Path, data: bytes) -> bool:
    try:
        return path.is_file() and path.stat().st_size == len(data) and path.read_bytes() == data
    except OSError:
        return False


def _save(directory: Path, prefix: str, name: str, data: bytes) -> Path:
    """Write `data` as `<prefix><name>` in `directory`, or find the file that already holds it.
    A different file of that name is never overwritten: the copy gets ` (2)`, ` (3)`, ...
    before the extension."""
    stem, ext = os.path.splitext(name)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for number in range(1, MAX_COPIES + 1):
            path = directory / f"{prefix}{stem}{'' if number == 1 else f' ({number})'}{ext}"
            if not os.path.lexists(path):
                config.atomic_write(path, data, mode=0o600)
                return path
            if _holds(path, data):
                return path
    except OSError as err:
        raise MailError(
            f"cannot save the attachment in {directory}: {err.strerror or err}") from None
    raise MailError(f"{directory} already holds {MAX_COPIES} different files of that name")


def _fetch(connect, mb: Mailbox, password: str, item: dict) -> bytes:
    """The raw message `item` from INBOX (BODY.PEEK: it stays unread)."""
    srv = None
    try:
        srv = connect(mb, password)
        if srv.inbox_status()[0] != item["uidvalidity"]:
            raise MailError(_stale(item["id"]))
        found = dict(srv.fetch([item["uid"]]))
    except MailError as err:
        raise MailError(_scrub(str(err), password)) from None
    except Exception as err:  # noqa: BLE001 - whatever the connection raised
        raise MailError(_scrub(f"{type(err).__name__}: {err}", password)) from None
    finally:
        if srv is not None:
            with contextlib.suppress(Exception):
                srv.close()
    if item["uid"] not in found:
        raise MailError(
            f"message {item['id']} is no longer in INBOX on the server (deleted or moved by "
            "another program); its attachments cannot be fetched")
    return found[item["uid"]]


def download_attachment(hub: Path, mail_dir: Path, id: int, index: int, *,
                        connect=None) -> Path:
    """Save attachment `index` (0-based, as listed by `bg read`) of message `id` and return its
    path: `<mail_dir>/attachments/<address file name>/<YYYY-MM>/<id>-<safe name>`.

    The message is fetched from INBOX with BODY.PEEK, so it stays unread. Downloading it again
    finds the file already there; a different file of the same name is never overwritten (the new
    one gets ` (2)`). Only mail still in INBOX can be fetched, so a message that was archived or
    deleted gives a MailError (an attachment downloaded before stays where it is)."""
    mail_dir = Path(mail_dir)
    _check_id(id)
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= MAX_NUMBER:
        raise MailError(f"there is no attachment {index}")
    with Store.open(mail_dir) as store:
        item = store.locate([id]).get(id)
        reason = _why_not(store, item, id, {})
    if reason is not None:
        raise MailError(reason)
    count = len(item["attachments"])
    if index >= count:
        raise MailError(
            f"message {id} has no attachments" if count == 0 else
            f"message {id} has {count} attachment(s), numbered from 0: there is no "
            f"attachment {index}")
    mb = _mailbox_of(config.load(mail_dir), item["address"])
    if mb is None:
        raise MailError(f"the mailbox {item['address']} is not in config.toml any more")
    password = accounts.read_password(Path(hub), mb.address)
    raw = _fetch(connect or server.connect, mb, password, item)
    filename, data = attachment_bytes(raw, index)
    directory = (mail_dir / ATTACHMENTS_DIR / accounts.address_file_name(item["address"])
                 / _month(item))
    return _save(directory, f"{id}-", safe_filename(filename, index), data)

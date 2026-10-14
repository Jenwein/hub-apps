"""The mail servers of one mailbox: IMAP to read and change mail, SMTP to send it.

`MailServer` is what the rest of mailkit uses; `connect(mb, password)` gives an
`ImapSmtpServer` (imap-tools over imaplib, and smtplib). Tests pass their own `connect` that
hands out an in-memory server instead (tests/mailfakes.py).

Rules that hold for every method of ImapSmtpServer:

- Nothing connects before it is needed: the IMAP connection opens and logs in at the first
  IMAP call; SMTP connects only to check the login or to send, and closes right after. Every
  network wait has a timeout (`TIMEOUT`).
- Reading never changes the mailbox: INBOX is opened with EXAMINE (read-only) and mail is
  fetched with BODY.PEEK[], so \\Seen stays as it was. Only `set_seen` and `move` open INBOX
  read-write (SELECT). Nothing is expunged but the very messages a move copied, and only by
  UID EXPUNGE; a plain EXPUNGE, which would also remove mail another client marked deleted, is
  never sent. A server with neither MOVE nor UIDPLUS keeps the originals, flagged \\Deleted,
  and `move` says so (`{"expunged": False}`).
- For NetEase (`send_id` in the provider table) the IMAP ID command goes out after LOGIN and
  before any folder is opened; NetEase answers "Unsafe Login" without it.
- Failures are MailErrors. `LoginError` when the server cannot be reached or refuses the login
  (its `step` says which: "connect", "imap-login", "smtp-login"); `ServerError` for anything
  that fails after login (a dropped connection, a timeout, a NO or BAD reply). Messages carry
  the server's reply and never the password.
- Production is always SSL (IMAP 993, SMTP 465); `ssl=False` exists only for local test servers.
"""

from __future__ import annotations

import contextlib
import imaplib
import re
import smtplib
import ssl as _ssl
import time
from collections.abc import Iterator
from datetime import date
from typing import Protocol, Self

from imap_tools import MailBox, MailBoxUnencrypted
from imap_tools.errors import ImapToolsError, MailboxLoginError, UnexpectedCommandStatusError
from imap_tools.utils import encode_folder

from .config import HOST_PORT, Mailbox, MailError
from .providers import PROVIDERS

TIMEOUT = 30.0  # seconds, for each network wait
LOGIN_STEPS = ("connect", "imap-login", "smtp-login")
ID_ARGUMENTS = '("name" "cc-hub-mail" "version" "0.1.0" "vendor" "hub-apps")'
CHUNK = 50  # UIDs per FETCH, STORE, COPY or MOVE command: bounded command lines and replies
REPLY_LIMIT = 500  # characters of a server reply kept in an error message
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_UID = re.compile(rb"\bUID (\d+)")

# imaplib knows ID only as an extension command; it is legal in every state (RFC 2971)
imaplib.Commands.setdefault("ID", ("NONAUTH", "AUTH", "SELECTED"))


class LoginError(MailError):
    """The server could not be reached (`step` "connect") or refused the login ("imap-login",
    "smtp-login"). The message names the server and carries its reply."""

    def __init__(self, step: str, message: str):
        if step not in LOGIN_STEPS:
            raise ValueError(f"step must be one of {LOGIN_STEPS}, got {step!r}")
        super().__init__(message)
        self.step = step


class ServerError(MailError):
    """A mail server operation failed after login: the connection dropped or timed out, or
    the server answered NO or BAD. The message carries the server's reply."""


class MailServer(Protocol):
    """The servers of one mailbox. Every UID is a UID in INBOX (the index holds INBOX mail);
    lists of UIDs come back in ascending order."""

    def check_login(self) -> None:
        """Log in to IMAP (and open INBOX read-only) and to SMTP; LoginError when either fails.
        Sends no mail."""

    def inbox_status(self) -> tuple[int, int]:
        """(UIDVALIDITY, UIDNEXT) of INBOX, fresh from the server."""

    def uids_after(self, uid: int) -> list[int]:
        """The UIDs in INBOX greater than `uid`."""

    def uids_since(self, day: date) -> list[int]:
        """The UIDs of INBOX mail that arrived on `day` or later (the server's internal date)."""

    def fetch(self, uids: list[int]) -> list[tuple[int, bytes]]:
        """(uid, raw message) for each of `uids` still in INBOX, without setting \\Seen."""

    def search_text(self, text: str, since: date | None) -> list[int]:
        """The UIDs of INBOX mail whose text or headers hold `text` (the server's own search;
        Chinese servers often find nothing for Chinese text)."""

    def set_seen(self, uids: list[int], seen: bool) -> None:
        """Set or clear \\Seen on INBOX messages."""

    def folders(self) -> list[tuple[str, tuple[str, ...]]]:
        """(name, attributes) of every folder, such as ("Trash", ("\\\\Trash",))."""

    def move(self, uids: list[int], folder: str) -> dict:
        """Move INBOX messages to `folder`, which must exist. Returns `{"expunged": bool}`:
        True when the originals are gone from INBOX (MOVE, or COPY and UID EXPUNGE); False
        when the server can do neither, so they are copied and only flagged \\Deleted in
        INBOX, where webmail and phone apps still show them."""

    def create_folder(self, name: str) -> None:
        """Create a folder; ServerError when it exists."""

    def append(self, folder: str, raw: bytes) -> None:
        """Store a message in `folder` (a copy of sent mail), marked \\Seen."""

    def send(self, raw: bytes, sender: str, recipients: list[str]) -> dict[str, str]:
        """Send `raw` as it is to `recipients` (the envelope; Bcc included) from `sender`.
        Returns the recipients the server refused, with its reply; {} when it took them all.
        ServerError when it took none."""

    def close(self) -> None:
        """Log out. Safe to call more than once."""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def connect(mb: Mailbox, password: str, *, ssl: bool = True,
            timeout: float = TIMEOUT) -> MailServer:
    """The servers of mailbox `mb`, logged in to with its address and `password` when first
    used. `ssl=False` speaks plain IMAP and SMTP: only for local test servers."""
    return ImapSmtpServer(mb, password, ssl=ssl, timeout=timeout)


def password_problem(password: str) -> str | None:
    """Why `password` cannot be used to log in, or None. Never quotes the password."""
    if not password:
        return "the password is empty"
    if not password.isascii() or not password.isprintable():
        return (
            "the password has characters a mail login cannot take (line breaks, tabs, control "
            "or non-ASCII characters); client passwords and authorization codes are ASCII "
            "letters, digits and punctuation, so check what was pasted"
        )
    return None


def split_host_port(value: str) -> tuple[str, int]:
    """("imap.qq.com", 993) for "imap.qq.com:993"; brackets around an IPv6 address go."""
    match = HOST_PORT.fullmatch(value)
    if match is None:
        raise MailError(f"{value!r} is not host:port")
    return match["host"].strip("[]"), int(match["port"])


# -- helpers ---------------------------------------------------------------------------------


def _decode(value) -> str:
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


def _reply(data) -> str:
    """The text of an IMAP reply's data, as one line."""
    items = data if isinstance(data, list | tuple) else [data]
    parts = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, tuple):  # a literal: say how big, do not quote it
            parts.append(_decode(item[0]))
        else:
            parts.append(_decode(item))
    return " ".join(" ".join(parts).split())


def _describe(err: BaseException) -> str:
    """What went wrong, for an error message: the server's reply where there is one."""
    if isinstance(err, UnexpectedCommandStatusError):
        typ, data = err.command_result[0], err.command_result[1]
        text = f"{typ} {_reply(data)}"
    elif isinstance(err, smtplib.SMTPResponseException):
        text = f"{err.smtp_code} {_decode(err.smtp_error)}"
    elif isinstance(err, smtplib.SMTPRecipientsRefused):
        text = "; ".join(
            f"{addr}: {code} {_decode(msg)}" for addr, (code, msg) in err.recipients.items())
    else:
        text = str(err) or type(err).__name__
    text = " ".join(text.split())
    return text[:REPLY_LIMIT] + ("..." if len(text) > REPLY_LIMIT else "")


def _imap_date(day: date) -> str:
    """An IMAP date, 14-Oct-2026, with English month names whatever the locale."""
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _chunks(uids: list[int], size: int = CHUNK) -> Iterator[list[int]]:
    for start in range(0, len(uids), size):
        yield uids[start : start + size]


def _uid_set(uids: list[int]) -> str:
    return ",".join(str(uid) for uid in uids)


def _wanted(uids) -> list[int]:
    return sorted({int(uid) for uid in uids})


def _number(values) -> int | None:
    if not values or values[-1] is None:
        return None
    try:
        return int(_decode(values[-1]).split()[0])
    except (ValueError, IndexError):
        return None


def _fetched(data) -> list[tuple[int, bytes]]:
    """(uid, raw) from the reply of `UID FETCH ... (UID BODY.PEEK[])` as imaplib returns it:
    a tuple (head with "UID n", literal) per message, then the rest of its line as bytes. Some
    servers put the UID after the literal, in that rest."""
    found: list[list] = []
    waiting = None  # the entry whose UID is still to come
    for item in data:
        if isinstance(item, tuple) and len(item) == 2:
            match = _UID.search(item[0])
            found.append([int(match.group(1)) if match else None, bytes(item[1])])
            waiting = None if match else len(found) - 1
        elif isinstance(item, bytes) and waiting is not None:
            match = _UID.search(item)
            if match:
                found[waiting][0] = int(match.group(1))
            waiting = None
    return [(uid, raw) for uid, raw in found if uid is not None]


def _shutdown(box) -> None:
    with contextlib.suppress(Exception):
        box.client.shutdown()


def _close_smtp(client: smtplib.SMTP) -> None:
    try:
        client.quit()
    except Exception:  # noqa: BLE001 - the connection may already be gone
        with contextlib.suppress(Exception):
            client.close()


# -- the real servers ------------------------------------------------------------------------


class ImapSmtpServer(MailServer):
    def __init__(self, mb: Mailbox, password: str, *, ssl: bool = True,
                 timeout: float = TIMEOUT):
        self.address = mb.address
        self.imap_host, self.imap_port = split_host_port(mb.imap)
        self.smtp_host, self.smtp_port = split_host_port(mb.smtp)
        provider = PROVIDERS.get(mb.provider)
        self.send_id = bool(provider and provider.send_id)
        self._password = password
        self._ssl = ssl
        self._timeout = timeout
        self._box = None  # the logged-in imap_tools mailbox
        self._selected: tuple[str, bool] | None = None  # (folder, read-only)
        self._caps: frozenset[str] | None = None

    def __repr__(self) -> str:
        return (f"ImapSmtpServer({self.address!r}, imap={self.imap_host}:{self.imap_port}, "
                f"smtp={self.smtp_host}:{self.smtp_port})")

    @property
    def _imap_at(self) -> str:
        return f"{self.imap_host}:{self.imap_port}"

    @property
    def _smtp_at(self) -> str:
        return f"{self.smtp_host}:{self.smtp_port}"

    def _scrub(self, text: str) -> str:
        return text.replace(self._password, "[password]") if self._password else text

    def _login_error(self, step: str, text: str) -> LoginError:
        return LoginError(step, self._scrub(text))

    def _server_error(self, text: str) -> ServerError:
        return ServerError(self._scrub(text))

    # -- IMAP connection --

    def _imap(self):
        """The logged-in IMAP connection, opened on first use."""
        if self._box is not None:
            return self._box
        problem = password_problem(self._password)
        if problem:
            raise LoginError("imap-login", f"cannot log in to {self.address}: {problem}")
        try:
            if self._ssl:
                box = MailBox(self.imap_host, self.imap_port, timeout=self._timeout,
                              ssl_context=_ssl.create_default_context())
            else:
                box = MailBoxUnencrypted(self.imap_host, self.imap_port, timeout=self._timeout)
        except (OSError, imaplib.IMAP4.error) as err:
            raise self._login_error(
                "connect", f"cannot reach the IMAP server {self._imap_at}: {_describe(err)}"
            ) from None
        try:
            box.login(self.address, self._password, initial_folder=None)
        except MailboxLoginError as err:
            _shutdown(box)
            raise self._login_error(
                "imap-login",
                f"the IMAP server {self._imap_at} refused the login: {_describe(err)}") from None
        except (OSError, imaplib.IMAP4.abort) as err:
            _shutdown(box)
            raise self._login_error(
                "connect", f"the IMAP server {self._imap_at} dropped the connection during "
                f"login: {_describe(err)}") from None
        except imaplib.IMAP4.error as err:  # BAD
            _shutdown(box)
            raise self._login_error(
                "imap-login",
                f"the IMAP server {self._imap_at} refused the login: {_describe(err)}") from None
        if self.send_id:
            try:
                box.client.xatom("ID", ID_ARGUMENTS)
            except (OSError, imaplib.IMAP4.abort) as err:
                _shutdown(box)
                raise self._login_error(
                    "connect", f"the IMAP server {self._imap_at} dropped the connection after "
                    f"login: {_describe(err)}") from None
            except imaplib.IMAP4.error:
                pass  # a server without ID answers BAD; it does not need it
        self._box = box
        return box

    def _drop(self) -> None:
        """Forget a connection that broke; the next call opens a new one."""
        box, self._box, self._selected, self._caps = self._box, None, None, None
        if box is not None:
            _shutdown(box)

    def _inbox(self, *, readonly: bool, fresh: bool = False):
        """The connection with INBOX open: EXAMINE when `readonly`, else SELECT."""
        box = self._imap()
        if fresh or self._selected != ("INBOX", readonly):
            self._selected = None
            box.folder.set("INBOX", readonly=readonly)
            self._selected = ("INBOX", readonly)
        return box

    def _capabilities(self) -> frozenset[str]:
        """What the server offers after login (Gmail, for one, names MOVE only then)."""
        if self._caps is None:
            client = self._imap().client
            caps = {str(c).upper() for c in client.capabilities}
            typ, data = client.capability()
            if typ == "OK":
                caps |= {word.upper() for item in data if item for word in _decode(item).split()}
            self._caps = frozenset(caps)
        return self._caps

    @contextlib.contextmanager
    def _guard(self, doing: str):
        """Turn what imaplib and imap-tools raise into ServerError."""
        try:
            yield
        except (LoginError, ServerError):
            raise
        except (OSError, imaplib.IMAP4.abort) as err:  # the connection is gone
            self._drop()
            raise self._server_error(
                f"{doing} on {self._imap_at} failed: {_describe(err)}") from None
        except (imaplib.IMAP4.error, ImapToolsError) as err:  # NO or BAD; still connected
            raise self._server_error(
                f"{doing} on {self._imap_at} failed: {_describe(err)}") from None

    def _ok(self, result, doing: str):
        typ, data = result[0], result[1]
        if typ != "OK":
            raise self._server_error(
                f"{doing} on {self._imap_at} failed: {typ} {_reply(data)}"[: REPLY_LIMIT + 200])
        return data

    def _search(self, client, *criteria) -> list[int]:
        data = self._ok(client.uid("SEARCH", *criteria), "searching INBOX")
        return sorted({int(n) for item in data if item for n in _decode(item).split()})

    # -- MailServer --

    def check_login(self) -> None:
        self._imap()
        try:
            self._inbox(readonly=True, fresh=True)
        except ImapToolsError as err:
            raise self._login_error(
                "imap-login", f"logged in to the IMAP server {self._imap_at}, but it refused "
                f"to open INBOX: {_describe(err)}") from None
        except (OSError, imaplib.IMAP4.abort) as err:
            self._drop()
            raise self._login_error(
                "connect", f"the IMAP server {self._imap_at} dropped the connection while "
                f"opening INBOX: {_describe(err)}") from None
        except imaplib.IMAP4.error as err:
            raise self._login_error(
                "imap-login", f"logged in to the IMAP server {self._imap_at}, but it refused "
                f"to open INBOX: {_describe(err)}") from None
        _close_smtp(self._smtp())

    def inbox_status(self) -> tuple[int, int]:
        with self._guard("reading the state of INBOX"):
            box = self._inbox(readonly=True, fresh=True)
            codes = box.client.untagged_responses
            uidvalidity = _number(codes.get("UIDVALIDITY"))
            uidnext = _number(codes.get("UIDNEXT"))
            if uidvalidity is None or uidnext is None:
                status = box.folder.status("INBOX", ["UIDVALIDITY", "UIDNEXT"])
                uidvalidity = uidvalidity if uidvalidity is not None else status.get("UIDVALIDITY")
                uidnext = uidnext if uidnext is not None else status.get("UIDNEXT")
            if uidnext is None:
                uidnext = max(self._search(box.client, "ALL"), default=0) + 1
            if uidvalidity is None:
                raise self._server_error(
                    f"the IMAP server {self._imap_at} does not tell the UIDVALIDITY of INBOX")
            return uidvalidity, uidnext

    def uids_after(self, uid: int) -> list[int]:
        with self._guard("searching INBOX"):
            box = self._inbox(readonly=True)
            # "n:*" also matches the last message when n is past it: keep only the newer ones
            return [u for u in self._search(box.client, "UID", f"{int(uid) + 1}:*") if u > uid]

    def uids_since(self, day: date) -> list[int]:
        with self._guard("searching INBOX"):
            box = self._inbox(readonly=True)
            return self._search(box.client, "SINCE", _imap_date(day))

    def fetch(self, uids: list[int]) -> list[tuple[int, bytes]]:
        wanted = _wanted(uids)
        if not wanted:
            return []
        found: dict[int, bytes] = {}
        with self._guard("fetching mail"):
            box = self._inbox(readonly=True)
            for chunk in _chunks(wanted):
                data = self._ok(
                    box.client.uid("FETCH", _uid_set(chunk), "(UID BODY.PEEK[])"),
                    "fetching mail")
                asked = set(chunk)
                for uid, raw in _fetched(data):
                    if uid in asked:
                        found[uid] = raw
        return [(uid, found[uid]) for uid in wanted if uid in found]

    def search_text(self, text: str, since: date | None) -> list[int]:
        criteria = ["SINCE", _imap_date(since)] if since is not None else []
        with self._guard("searching INBOX"):
            client = self._inbox(readonly=True).client
            if text.isascii() and text.isprintable():
                return self._search(client, *criteria, "TEXT", _quote(text))
            # anything else goes as a UTF-8 literal, the only way IMAP takes it
            client.literal = text.encode("utf-8")
            return self._search(client, "CHARSET", "UTF-8", *criteria, "TEXT")

    def set_seen(self, uids: list[int], seen: bool) -> None:
        wanted = _wanted(uids)
        if not wanted:
            return
        with self._guard("marking mail read" if seen else "marking mail unread"):
            client = self._inbox(readonly=False).client
            for chunk in _chunks(wanted):
                self._ok(client.uid("STORE", _uid_set(chunk),
                                    "+FLAGS.SILENT" if seen else "-FLAGS.SILENT", r"(\Seen)"),
                         "changing \\Seen")

    def folders(self) -> list[tuple[str, tuple[str, ...]]]:
        with self._guard("listing the folders"):
            return [(f.name, tuple(f.flags)) for f in self._imap().folder.list()]

    def move(self, uids: list[int], folder: str) -> dict:
        wanted = _wanted(uids)
        if not wanted:
            return {"expunged": True}  # nothing left behind
        doing = f"moving mail to {folder}"
        with self._guard(doing):
            caps = self._capabilities()
            client = self._inbox(readonly=False).client
            target = encode_folder(folder)
            expunged = "MOVE" in caps or "UIDPLUS" in caps
            for chunk in _chunks(wanted):
                ids = _uid_set(chunk)
                if "MOVE" in caps:
                    self._ok(client.uid("MOVE", ids, target), doing)
                    continue
                # no MOVE: copy, mark the originals deleted, and expunge exactly those; a plain
                # EXPUNGE would also erase mail another client marked deleted, so without
                # UIDPLUS the originals stay, flagged
                self._ok(client.uid("COPY", ids, target), doing)
                self._ok(client.uid("STORE", ids, "+FLAGS.SILENT", r"(\Deleted)"), doing)
                if "UIDPLUS" in caps:
                    self._ok(client.uid("EXPUNGE", ids), doing)
        return {"expunged": expunged}

    def create_folder(self, name: str) -> None:
        with self._guard(f"creating the folder {name}"):
            self._imap().folder.create(name)

    def append(self, folder: str, raw: bytes) -> None:
        doing = f"saving a copy in {folder}"
        with self._guard(doing):
            client = self._imap().client
            self._ok(client.append(encode_folder(folder), r"(\Seen)",
                                   imaplib.Time2Internaldate(time.time()), raw), doing)

    # -- SMTP --

    def _smtp(self) -> smtplib.SMTP:
        """A logged-in SMTP connection; the caller closes it."""
        problem = password_problem(self._password)
        if problem:
            raise LoginError("smtp-login", f"cannot log in to {self.address}: {problem}")
        try:
            if self._ssl:
                client = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=self._timeout,
                                          context=_ssl.create_default_context())
            else:
                client = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self._timeout)
        except OSError as err:  # smtplib's own errors are OSErrors too
            raise self._login_error(
                "connect", f"cannot reach the SMTP server {self._smtp_at}: {_describe(err)}"
            ) from None
        try:
            client.login(self.address, self._password)
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPNotSupportedError) as err:
            _close_smtp(client)
            raise self._login_error(
                "smtp-login",
                f"the SMTP server {self._smtp_at} refused the login: {_describe(err)}") from None
        except smtplib.SMTPServerDisconnected as err:
            _close_smtp(client)
            raise self._login_error(
                "connect", f"the SMTP server {self._smtp_at} dropped the connection during "
                f"login: {_describe(err)}") from None
        except smtplib.SMTPException as err:
            _close_smtp(client)
            raise self._login_error(
                "smtp-login",
                f"the SMTP server {self._smtp_at} refused the login: {_describe(err)}") from None
        except OSError as err:
            _close_smtp(client)
            raise self._login_error(
                "connect", f"the SMTP server {self._smtp_at} dropped the connection during "
                f"login: {_describe(err)}") from None
        return client

    def send(self, raw: bytes, sender: str, recipients: list[str]) -> dict[str, str]:
        recipients = list(recipients)
        if not recipients:
            raise MailError("nothing to send: the message has no recipient")
        client = self._smtp()
        options = [] if all(a.isascii() for a in (sender, *recipients)) else ["SMTPUTF8"]
        try:
            refused = client.sendmail(sender, recipients, raw, mail_options=options)
        except smtplib.SMTPRecipientsRefused as err:
            raise self._server_error(
                f"the SMTP server {self._smtp_at} refused every recipient: {_describe(err)}"
            ) from None
        except smtplib.SMTPSenderRefused as err:
            raise self._server_error(
                f"the SMTP server {self._smtp_at} refused the sender {sender}: {_describe(err)}"
            ) from None
        except smtplib.SMTPDataError as err:
            raise self._server_error(
                f"the SMTP server {self._smtp_at} refused the message: {_describe(err)}"
            ) from None
        except OSError as err:  # smtplib's own errors included
            raise self._server_error(
                f"sending through {self._smtp_at} failed: {_describe(err)}") from None
        finally:
            _close_smtp(client)
        return {addr: f"{code} {_decode(msg)}" for addr, (code, msg) in refused.items()}

    def close(self) -> None:
        box, self._box, self._selected, self._caps = self._box, None, None, None
        if box is not None:
            try:
                box.client.logout()
            except Exception:  # noqa: BLE001 - leaving anyway
                _shutdown(box)

"""Test doubles for the mail app.

`FakeServer` is an in-memory `MailServer`: an INBOX, other folders, flags and a list of sent
mail, with the same rules as `ImapSmtpServer` where tests rely on them (logins fail with
`LoginError` at the step that is set to fail, a failure after login is `ServerError`, fetching
never sets `\\Seen`). Every call is recorded in `calls`, so a test can check that the background
poll never changes server state (`server.writes()` is empty).

`fake_connect(server)` is the `connect=` argument for code under test: one FakeServer for every
mailbox, or a dict of them by address or mailbox name.

`make_raw(...)` builds the raw bytes of a message, as a server would hand them over.
"""

from __future__ import annotations

import mimetypes
from collections import namedtuple
from datetime import UTC, date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime, make_msgid

from mailkit.config import Mailbox
from mailkit.message import parse
from mailkit.server import LOGIN_STEPS, LoginError, MailServer, ServerError

# a message handed to send(): the bytes, the envelope sender and the envelope recipients
SentMail = namedtuple("SentMail", "raw sender recipients")

# the Date of a message made by make_raw without a date
DEFAULT_DATE = datetime(2026, 10, 14, 9, 0, tzinfo=timezone(timedelta(hours=8)))

# the methods that change something on the server
WRITES = ("set_seen", "move", "create_folder", "append", "send")


class FakeServer(MailServer):
    """An in-memory mail server for one mailbox.

    - `inbox`: INBOX messages by UID; `flags`: their flags (`\\Seen`, ...) by UID.
    - `folders_`: the other folders by name, each `{uid: raw}`; `folder_flags`: the LIST
      attributes of a folder (`{"Trash": ("\\\\Trash",)}`), for SPECIAL-USE lookups.
    - `received`: the internal date of an INBOX message by UID, which `uids_since` uses
      (default: the message's Date header, else DEFAULT_DATE).
    - `uidvalidity`, `uidnext`: what `inbox_status` answers. `deliver` takes the next UID.
    - `fail_login`: "connect", "imap-login" or "smtp-login" makes that step fail with LoginError,
      in check_login and in every operation that needs it (IMAP operations need connect and
      imap-login; send needs connect and smtp-login).
    - `password`: when set, a login with another password fails at "imap-login" (and at
      "smtp-login" for send).
    - `error`: when set, every operation after login raises it (a dropped connection, say
      `ServerError("connection reset")`).
    - `caps`: the IMAP capabilities that matter to `move` (default {"MOVE", "UIDPLUS"}). With
      neither, `move` copies, leaves the originals in INBOX flagged `\\Deleted` and returns
      `{"expunged": False}`, as ImapSmtpServer does on such a server.
    - `sent`: a SentMail for each send; `calls`: (method, arguments...) for each call.
    """

    def __init__(
        self,
        inbox: dict[int, bytes] | None = None,
        *,
        folders: dict[str, dict[int, bytes]] | None = None,
        folder_flags: dict[str, tuple[str, ...]] | None = None,
        flags: dict[int, set[str]] | None = None,
        received: dict[int, datetime | date] | None = None,
        uidvalidity: int = 1,
        fail_login: str | None = None,
        password: str | None = None,
        error: Exception | None = None,
        caps: set[str] | frozenset[str] = frozenset({"MOVE", "UIDPLUS"}),
    ):
        if fail_login is not None and fail_login not in LOGIN_STEPS:
            raise ValueError(f"fail_login must be one of {LOGIN_STEPS}, got {fail_login!r}")
        self.inbox: dict[int, bytes] = dict(inbox or {})
        self.folders_: dict[str, dict[int, bytes]] = {
            name: dict(messages) for name, messages in (folders or {}).items()}
        self.folder_flags: dict[str, tuple[str, ...]] = dict(folder_flags or {})
        self.flags: dict[int, set[str]] = {uid: set(f) for uid, f in (flags or {}).items()}
        self.received: dict[int, datetime | date] = dict(received or {})
        self.uidvalidity = uidvalidity
        self.uidnext = max(self.inbox, default=0) + 1
        self.fail_login = fail_login
        self.password = password
        self.error = error
        self.caps = set(caps)
        self.sent: list[SentMail] = []
        self.calls: list[tuple] = []
        self.logins: list[tuple[str, str]] = []  # (address, password) of each connect
        self.mailbox: Mailbox | None = None  # the Mailbox of the last connect
        self.given_password: str | None = None
        self.closed = False

    # -- helpers for tests ---------------------------------------------------------------------

    def deliver(self, raw: bytes, *, received: datetime | date | None = None,
                flags: tuple[str, ...] = ()) -> int:
        """Put a new message in INBOX under the next UID; returns the UID."""
        uid = self.uidnext
        self.uidnext += 1
        self.inbox[uid] = raw
        if received is not None:
            self.received[uid] = received
        if flags:
            self.flags[uid] = set(flags)
        return uid

    def writes(self) -> list[tuple]:
        """The calls that change something on the server."""
        return [call for call in self.calls if call[0] in WRITES]

    def names(self) -> list[str]:
        """The names of the methods called, in order."""
        return [call[0] for call in self.calls]

    # -- the rules ---------------------------------------------------------------------------

    def _wrong_password(self) -> bool:
        return self.password is not None and self.given_password != self.password

    def _login(self, *, imap: bool, smtp: bool) -> None:
        if self.fail_login == "connect":
            raise LoginError("connect", "cannot reach imap.fake.example:993: connection refused")
        if imap and (self.fail_login == "imap-login" or self._wrong_password()):
            raise LoginError(
                "imap-login",
                "the IMAP server imap.fake.example:993 refused the login: "
                "[AUTHENTICATIONFAILED] Invalid credentials")
        if smtp and (self.fail_login == "smtp-login" or self._wrong_password()):
            raise LoginError(
                "smtp-login",
                "the SMTP server smtp.fake.example:465 refused the login: "
                "535 Error: authentication failed")

    def _ready(self, *, imap: bool = True, smtp: bool = False) -> None:
        self._login(imap=imap, smtp=smtp)
        if self.error is not None:
            raise self.error

    def _received(self, uid: int) -> date:
        value = self.received.get(uid)
        if value is None:
            iso = parse(self.inbox[uid]).date
            value = datetime.fromisoformat(iso) if iso else DEFAULT_DATE
        if isinstance(value, datetime):
            return value.astimezone(UTC).date() if value.tzinfo else value.date()
        return value

    def _folder(self, name: str) -> dict[int, bytes]:
        if name == "INBOX":
            return self.inbox
        if name not in self.folders_:
            raise ServerError(f"NO [TRYCREATE] there is no folder {name!r}")
        return self.folders_[name]

    # -- MailServer --------------------------------------------------------------------------

    def check_login(self) -> None:
        self.calls.append(("check_login",))
        self._login(imap=True, smtp=True)

    def inbox_status(self) -> tuple[int, int]:
        self.calls.append(("inbox_status",))
        self._ready()
        return self.uidvalidity, self.uidnext

    def uids_after(self, uid: int) -> list[int]:
        self.calls.append(("uids_after", uid))
        self._ready()
        return sorted(u for u in self.inbox if u > uid)

    def uids_since(self, day: date) -> list[int]:
        self.calls.append(("uids_since", day))
        self._ready()
        return sorted(u for u in self.inbox if self._received(u) >= day)

    def fetch(self, uids: list[int]) -> list[tuple[int, bytes]]:
        self.calls.append(("fetch", tuple(uids)))
        self._ready()
        return [(uid, self.inbox[uid]) for uid in sorted(set(uids)) if uid in self.inbox]

    def search_text(self, text: str, since: date | None) -> list[int]:
        self.calls.append(("search_text", text, since))
        self._ready()
        needle = text.lower()
        found = []
        for uid, raw in sorted(self.inbox.items()):
            if since is not None and self._received(uid) < since:
                continue
            p = parse(raw)
            hay = " ".join([p.subject, p.from_addr, p.from_name, p.text, *p.to, *p.cc]).lower()
            if needle in hay:
                found.append(uid)
        return found

    def set_seen(self, uids: list[int], seen: bool) -> None:
        self.calls.append(("set_seen", tuple(uids), seen))
        self._ready()
        for uid in uids:
            if uid in self.inbox:
                marks = self.flags.setdefault(uid, set())
                if seen:
                    marks.add("\\Seen")
                else:
                    marks.discard("\\Seen")

    def folders(self) -> list[tuple[str, tuple[str, ...]]]:
        self.calls.append(("folders",))
        self._ready()
        return [("INBOX", self.folder_flags.get("INBOX", ()))] + [
            (name, self.folder_flags.get(name, ())) for name in self.folders_]

    def move(self, uids: list[int], folder: str) -> dict:
        self.calls.append(("move", tuple(uids), folder))
        if not uids:
            return {"expunged": True}  # like ImapSmtpServer: nothing to do, nothing sent
        self._ready()
        target = self._folder(folder)
        expunged = bool({"MOVE", "UIDPLUS"} & self.caps)
        for uid in sorted(set(uids)):
            if uid not in self.inbox or target is self.inbox:
                continue
            target[max(target, default=0) + 1] = self.inbox[uid]
            if expunged:
                del self.inbox[uid]
                self.flags.pop(uid, None)
            else:
                self.flags.setdefault(uid, set()).add("\\Deleted")
        return {"expunged": expunged}

    def create_folder(self, name: str) -> None:
        self.calls.append(("create_folder", name))
        self._ready()
        if name == "INBOX" or name in self.folders_:
            raise ServerError(f"NO [ALREADYEXISTS] the folder {name!r} exists")
        self.folders_[name] = {}

    def append(self, folder: str, raw: bytes) -> None:
        self.calls.append(("append", folder, raw))
        self._ready()
        if folder == "INBOX":
            self.deliver(raw, flags=("\\Seen",))
        else:
            target = self._folder(folder)
            target[max(target, default=0) + 1] = raw

    def send(self, raw: bytes, sender: str, recipients: list[str]) -> dict[str, str]:
        self.calls.append(("send", raw, sender, tuple(recipients)))
        self._ready(imap=False, smtp=True)
        self.sent.append(SentMail(raw, sender, list(recipients)))
        return {}

    def close(self) -> None:
        self.calls.append(("close",))
        self.closed = True


def fake_connect(servers):
    """A `connect(mb, password, **options)` that hands out FakeServers instead of connecting.

    `servers` is one FakeServer for every mailbox, or a dict of FakeServers by address or by
    mailbox name. Each connect records the address and password in `server.logins`."""

    def connect(mb: Mailbox, password: str, **_options) -> FakeServer:
        if isinstance(servers, FakeServer):
            server = servers
        else:
            server = servers[mb.address] if mb.address in servers else servers[mb.name]
        server.logins.append((mb.address, password))
        server.given_password = password
        server.mailbox = mb
        server.closed = False
        return server

    return connect


def make_raw(
    subject: str,
    from_: str,
    to: str | list[str] | tuple[str, ...],
    body: str,
    *,
    cc: str | list[str] | tuple[str, ...] = (),
    date: datetime | str | None = None,
    attachments=(),
    headers: dict[str, str] | None = None,
) -> bytes:
    """The raw bytes (CRLF line endings) of a text/plain UTF-8 message.

    `date` is a datetime or the text of a Date header (default: DEFAULT_DATE). `attachments`
    holds `(filename, data)` or `(filename, data, "type/subtype")`. `headers` adds headers; a
    Message-ID among them replaces the generated one.
    """
    msg = EmailMessage()
    msg["From"] = from_
    msg["To"] = to if isinstance(to, str) else ", ".join(to)
    if cc:
        msg["Cc"] = cc if isinstance(cc, str) else ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = date if isinstance(date, str) else format_datetime(date or DEFAULT_DATE)
    extra = dict(headers or {})
    message_id = next((extra.pop(k) for k in list(extra) if k.lower() == "message-id"), None)
    msg["Message-ID"] = message_id or make_msgid(domain="test.example")
    for key, value in extra.items():
        msg[key] = value
    msg.set_content(body)
    for item in attachments:
        filename, data, *kind = item
        ctype = kind[0] if kind else (
            mimetypes.guess_type(filename)[0] or "application/octet-stream")
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg.as_bytes(policy=SMTP)

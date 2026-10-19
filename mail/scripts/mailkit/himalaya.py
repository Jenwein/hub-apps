"""The himalaya command line, and the one place that reads what it prints.

`Himalaya` runs one himalaya command per method, always in the state directory with the config
named by a relative file name (himalaya splits `-c` at colons, so `C:\\...` would break) and with
stdin closed unless the call hands it a mail. The parsers read every key through `norm`, so the
kebab-case of v2.2.1, snake_case and the camelCase of 3.0 all work. No method runs `message
delete`, `imap expunge` or `--seen`: "delete" is a move to the trash folder, and a mail is marked
as read only by `mark_seen`. `run` refuses those commands (see FORBIDDEN) whoever asks.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import signal
import subprocess
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime
from itertools import pairwise
from typing import NoReturn

from .configs import HIMALAYA_CONFIG
from .context import Ctx
from .settings import MailError
from .tools import NO_WINDOW

# --- errors ------------------------------------------------------------------------------------


class HimalayaError(MailError):
    """himalaya failed, or printed something mx cannot read."""


class AuthError(HimalayaError):
    """The server refused the login: the password or the sign-in is wrong or gone."""


class ConnectError(HimalayaError):
    """The server could not be reached, or did not answer in time."""


# Lowercase substrings of what himalaya prints, except `535` (the SMTP code), which counts only as
# a number of its own: `message 15350 not found` is no login failure. The last five connection
# patterns are the locale-free Winsock error numbers, since Windows words the rest in the language
# of the system (a refused connection reads `... (os error 10061)` after a Chinese sentence there).
AUTH_PATTERNS = (
    "authenticationfailed", "authentication failed", "invalid credentials", "login failed",
    "535", "invalid_grant", "missing refresh token", "no password stored",
)
CONNECT_PATTERNS = (
    "connection refused", "timed out", "failed to lookup address", "network is unreachable",
    "connection reset", "dns error",
    "os error 10061", "os error 10060", "os error 10051", "os error 10054", "os error 11001",
)


def _found(pattern: str, low: str) -> bool:
    if pattern.isdigit():  # a number: not a piece of a longer number, word or host name
        return re.search(rf"(?<![0-9a-z]){pattern}(?![0-9a-z])", low) is not None
    return pattern in low


def classify(text: str) -> type[HimalayaError]:
    """The error class of what himalaya printed."""
    low = text.lower()
    if any(_found(pattern, low) for pattern in AUTH_PATTERNS):
        return AuthError
    if any(_found(pattern, low) for pattern in CONNECT_PATTERNS):
        return ConnectError
    return HimalayaError


def kind(error: type[HimalayaError]) -> str:
    """`auth`, `connect` or `other`: how `mx check` names a class of error."""
    if issubclass(error, AuthError):
        return "auth"
    return "connect" if issubclass(error, ConnectError) else "other"


def parse_error(stdout: str) -> str:
    """The message of a failed call, from what himalaya printed (F-CLI): with `--json` the `error`
    and the `sources` joined with `: `; without it the `Error:` line (no `Error: ` prefix) and the
    `Caused by:` items joined the same way, up to `Suggestions:`; else the first non-empty line."""
    try:
        data = json.loads(stdout)
    except ValueError:
        data = None
    if isinstance(data, dict):
        found = _get(data, "error")
        if isinstance(found, str) and found:
            sources = _get(data, "sources")
            more = [s for s in sources if isinstance(s, str) and s] if isinstance(
                sources, list) else []
            return ": ".join([found, *more])
    lines = [line.strip() for line in stdout.splitlines()]
    head = next((line for line in lines if line), "")
    if not head.startswith("Error:"):
        return head
    parts = [head[len("Error:"):].strip()]
    causes = False
    for line in lines[lines.index(head) + 1:]:
        if line.startswith("Suggestions:"):
            break
        if line == "Caused by:":
            causes = True
        elif causes and line.startswith("- "):
            parts.append(line[2:].strip())
        elif causes and line:  # a cause that goes on over several lines
            parts[-1] += "\n" + line
    return ": ".join(part for part in parts if part) or head


# --- reading JSON ------------------------------------------------------------------------------


def norm(key: str) -> str:
    """A key without case, `-` and `_`: `has-attachment`, `has_attachment` and `hasAttachment`
    read alike."""
    return key.lower().replace("-", "").replace("_", "")


class _Shape(Exception):
    """The JSON is not the shape the parser expects; the public parsers turn it into an
    HimalayaError that quotes the whole answer."""


def _excerpt(text: str) -> str:
    """The first 200 characters, with control and format characters (newlines, bidi marks)
    turned into spaces: this ends up in an error shown to the agent."""
    clean = "".join(" " if unicodedata.category(c).startswith("C") else c for c in text[:200])
    return clean[:200]


def _redacted(data: object) -> object:
    """`data` without the `body` of any message: an error must not carry mail."""
    if isinstance(data, dict):
        return {k: "..." if norm(k) == "body" else _redacted(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_redacted(v) for v in data]
    return data


def _bad(data: object) -> NoReturn:
    """The error for an answer of an unexpected shape, quoting the start of it."""
    text = data if isinstance(data, str) else json.dumps(
        _redacted(data), ensure_ascii=False, separators=(",", ":"), default=str)
    raise HimalayaError(f"himalaya printed something mx does not understand: {_excerpt(text)}")


def _get(obj: dict, name: str) -> object:
    """The value of the key that reads like `name` (already normalised), else None."""
    for key, value in obj.items():
        if norm(key) == name:
            return value
    return None


def _obj(value: object) -> dict:
    if not isinstance(value, dict):
        raise _Shape
    return value


def _arr(value: object) -> list:
    if not isinstance(value, list):
        raise _Shape
    return value


def _int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _Shape
    return value


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _date(value: object) -> datetime | None:
    """An RFC 3339 time, or None for a missing or unreadable one."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# --- envelopes and folders ---------------------------------------------------------------------


@dataclass(frozen=True)
class Envelope:
    id: str  # the IMAP UID
    subject: str
    from_name: str
    from_addr: str
    date: datetime | None  # the Date header
    seen: bool
    has_attachment: bool | None  # filled by `search`, which asks for it


@dataclass(frozen=True)
class Folder:
    name: str  # as the server gives it: for aliases and `-m`
    shown: str  # `name` decoded from IMAP's modified UTF-7
    role: str | None


def _is_seen(flags: object) -> bool:
    for flag in () if flags is None else _arr(flags):
        names = (_get(flag, "iana"), _get(flag, "raw")) if isinstance(flag, dict) else (flag,)
        if any(isinstance(n, str) and n.lstrip("\\").lower() == "seen" for n in names):
            return True
    return False


def _envelope(item: object) -> Envelope:
    obj = _obj(item)
    ident = _get(obj, "id")
    if isinstance(ident, int) and not isinstance(ident, bool):
        ident = str(ident)
    if not isinstance(ident, str) or not ident:
        raise _Shape
    senders = _get(obj, "from")
    first = _obj(_arr(senders)[0]) if senders else {}
    attachment = _get(obj, "hasattachment")
    return Envelope(
        id=ident,
        subject=_text(_get(obj, "subject")),
        from_name=_text(_get(first, "name")),
        from_addr=_text(_get(first, "email")),
        date=_date(_get(obj, "date")),
        seen=_is_seen(_get(obj, "flags")),
        has_attachment=attachment if isinstance(attachment, bool) else None,
    )


def parse_envelopes(data: object) -> list[Envelope]:
    """The envelopes of `envelope search` (F-QUERY)."""
    try:
        return [_envelope(item) for item in _arr(_get(_obj(data), "envelopes"))]
    except _Shape:
        _bad(data)


def _is_b64_run(chunk: str) -> bool:
    return all(c.isascii() and (c.isalnum() or c in "+,") for c in chunk)


def decode_utf7(name: str) -> str:
    """A folder name from IMAP's modified UTF-7 (RFC 3501 5.1.3) to text. A name that is not
    valid modified UTF-7 comes back as it is."""
    out: list[str] = []
    at = 0
    while at < len(name):
        char = name[at]
        if char != "&":
            if not " " <= char <= "~":  # outside a shift only printable ASCII is allowed
                return name
            out.append(char)
            at += 1
            continue
        end = name.find("-", at + 1)
        if end < 0:
            return name
        chunk = name[at + 1:end]
        if not chunk:
            out.append("&")  # `&-` is a literal &
        else:
            if not _is_b64_run(chunk):
                return name
            padded = chunk.replace(",", "/")
            padded += "=" * (-len(padded) % 4)
            try:
                raw = base64.b64decode(padded, validate=True)
                if len(raw) % 2:
                    return name
                out.append(raw.decode("utf-16-be"))
            except ValueError:  # bad base64, or UTF-16 with a lone surrogate
                return name
        at = end + 1
    return "".join(out)


def parse_folders(data: object) -> list[Folder]:
    """The folders of `mailbox list` (F-LIST)."""
    try:
        found = []
        for item in _arr(_get(_obj(data), "mailboxes")):
            obj = _obj(item)
            name = _get(obj, "name")
            if not isinstance(name, str) or not name:
                raise _Shape
            role = _get(obj, "role")
            found.append(Folder(name, decode_utf7(name), role if isinstance(role, str) else None))
        return found
    except _Shape:
        _bad(data)


# Folder names the providers use, matched on the decoded name without regard to case. IMAP has
# no SPECIAL-USE roles in himalaya's `mailbox list` (F-HCONF): these guesses are all there is.
KNOWN_FOLDERS: dict[str, tuple[str, ...]] = {
    "sent": ("Sent Messages", "Sent", "Sent Items", "Sent Mail", "[Gmail]/Sent Mail",
             "[Gmail]/已发邮件", "已发送"),
    "trash": ("Deleted Messages", "Trash", "Deleted", "Deleted Items", "[Gmail]/Trash",
              "[Gmail]/已删除邮件", "已删除"),
    "archive": ("Archive", "[Gmail]/All Mail", "归档"),
}


def guess_folders(folders: list[Folder]) -> dict[str, str | None]:
    """For `sent`, `trash` and `archive`: the `name` of the folder that carries a known name (the
    earlier one of KNOWN_FOLDERS wins), or None."""
    by_shown = {f.shown.casefold(): f.name for f in reversed(folders)}  # the first of twins wins
    return {
        role: next((by_shown[k.casefold()] for k in known if k.casefold() in by_shown), None)
        for role, known in KNOWN_FOLDERS.items()}


# --- running himalaya --------------------------------------------------------------------------


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill himalaya and what it started (the password command): a child that lives on would keep
    our pipes open. POSIX: the process group, since himalaya runs in its own session. Windows:
    taskkill walks the tree."""
    if os.name == "posix":
        with suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    else:
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)], check=False,
                stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                creationflags=NO_WINDOW)
    with suppress(OSError):
        proc.kill()
    with suppress(OSError, subprocess.SubprocessError):
        proc.communicate(timeout=10)


# F-NODEL: what mx never runs, in every spelling himalaya knows. Deleting for good (inside the
# trash too), expunging (CLOSE expunges as well), `imap raw` (it could send any of them), and
# `--seen`, which marks a mail as read (as a filter of `imap search` it only selects).
FORBIDDEN = frozenset({
    ("message", "delete"), ("message", "rm"), ("msg", "delete"), ("msg", "rm"),
    ("imap", "delete"), ("imap", "expunge"), ("imap", "close"), ("imap", "raw"),
})


def forbidden(args: tuple[str, ...]) -> str | None:
    """The forbidden command that `args` contain, as it is spelled there, else None."""
    words = [arg.lower() for arg in args]
    for pair in pairwise(words):
        if pair in FORBIDDEN:
            return " ".join(pair)
    if "--seen" in words and words[:2] != ["imap", "search"]:
        return "--seen"
    return None


def _first_line(data: bytes) -> str:
    for line in data.decode("utf-8", "replace").splitlines():
        if line.strip():
            return line.strip()
    return ""


class Himalaya:
    """himalaya for the mailboxes of `ctx`. `exe` names the program (default: `himalaya` on PATH,
    found with shutil.which since a `.cmd` shim cannot be started by bare name on Windows)."""

    def __init__(self, ctx: Ctx, exe: str | None = None, timeout: float = 90.0):
        self.ctx = ctx
        self.exe = exe
        self.timeout = timeout

    def executable(self) -> str:
        found = shutil.which(self.exe or "himalaya")
        if found is None:
            raise MailError(
                f"{self.exe or 'himalaya'} is not installed: run mx install-tool himalaya "
                "(uv run apps/mail/scripts/mx.py install-tool himalaya)")
        return found

    def run(
        self, mailbox: str, *args: str, input: bytes | None = None, json: bool = True
    ) -> bytes:
        """`himalaya -c himalaya.toml -a <mailbox> [--json] <args>` in the state directory; the
        bytes it printed. Stdin is the null device unless `input` is given. A non-zero exit is an
        HimalayaError (an AuthError or ConnectError when the text says so) with the message from
        stdout; no answer within the timeout kills it and is a ConnectError. A forbidden command
        (FORBIDDEN) is refused before anything runs, and so is any call while the machine has no
        himalaya.toml: himalaya would tell the caller to run its own `himalaya configure`."""
        refused = forbidden(args)
        if refused:
            raise MailError(f"mx never runs himalaya {refused}: mail is never deleted for good")
        exe = self.executable()
        if not (self.ctx.state / HIMALAYA_CONFIG).is_file():
            raise MailError("this machine has no mail config yet: run mx configure")
        argv = [exe, "-c", HIMALAYA_CONFIG, "-a", mailbox]
        if json:
            argv.append("--json")
        argv.extend(args)
        extra = {"start_new_session": True} if os.name == "posix" else {}
        try:
            proc = subprocess.Popen(
                argv, cwd=self.ctx.state,
                stdin=subprocess.DEVNULL if input is None else subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW,
                **extra)
        except OSError as err:
            raise MailError(f"cannot run himalaya: {err}") from err
        try:
            out, err = proc.communicate(input, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            raise ConnectError(f"himalaya did not answer within {self.timeout:g} s") from None
        except BaseException:  # Ctrl+C: do not leave himalaya running
            _kill_tree(proc)
            raise
        if proc.returncode != 0:
            shown, logged = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
            text = (
                parse_error(shown) or _first_line(err)
                or f"himalaya exited with status {proc.returncode}")
            raise classify(f"{shown}\n{logged}")(text[:300])
        return out

    def _answer(self, mailbox: str, *args: str) -> object:
        """The JSON that a command printed."""
        out = self.run(mailbox, *args)
        try:
            return json.loads(out)
        except ValueError:  # not JSON, or bytes that are not UTF-8
            _bad(out.decode("utf-8", "replace"))

    def folders(self, mailbox: str) -> list[Folder]:
        return parse_folders(self._answer(mailbox, "mailbox", "list"))

    def search(
        self, mailbox: str, folder: str, query: list[str], limit: int
    ) -> list[Envelope]:
        """The newest `limit` envelopes of `folder` that the query words select, with
        `has_attachment` filled in (`--has-attachment` is a flag, so it comes before the query)."""
        return parse_envelopes(self._answer(
            mailbox, "envelope", "search", "-m", folder, "-s", str(limit), "-p", "1",
            "--has-attachment", *query, "order", "by", "date", "desc"))

    def status(self, mailbox: str, folder: str) -> tuple[str, int]:
        """`(uid_validity, uid_next)` of `folder`."""
        data = self._answer(mailbox, "imap", "status", folder)
        try:
            obj = _obj(data)
            return str(_int(_get(obj, "uidvalidity"))), _int(_get(obj, "uidnext"))
        except _Shape:
            _bad(data)

    def fetch_dates(self, mailbox: str, folder: str, uid_set: str) -> dict[int, datetime | None]:
        """The received time of each mail in `uid_set` (`n:*` always answers the highest mail,
        even when nothing is newer than `n`)."""
        data = self._answer(mailbox, "imap", "fetch", "-m", folder, uid_set, "--internal-date")
        try:
            found: dict[int, datetime | None] = {}
            for item in _arr(_get(_obj(data), "messages")):
                obj = _obj(item)
                uid = _get(obj, "uid")
                if uid is not None:
                    found[_int(uid)] = _date(_get(obj, "internaldate"))
            return found
        except _Shape:
            _bad(data)

    def uids_since(self, mailbox: str, folder: str, day: date) -> list[int]:
        """The UIDs of the mails received on or after `day`, oldest first."""
        data = self._answer(
            mailbox, "imap", "search", "-m", folder, "--since", day.isoformat())
        try:
            obj = _obj(data)
            if _get(obj, "uidmode") is False:  # message numbers, not UIDs
                raise _Shape
            return sorted(_int(_get(_obj(item), "id")) for item in _arr(_get(obj, "ids")))
        except _Shape:
            _bad(data)

    def fetch_raw(self, mailbox: str, folder: str, uids: list[int]) -> dict[int, bytes]:
        """The exact bytes of each mail, in one IMAP session and without marking it as read. A
        UID the server does not have is left out."""
        if not uids:
            return {}
        data = self._answer(
            mailbox, "imap", "fetch", "-m", folder, ",".join(str(u) for u in uids), "--body")
        try:
            found: dict[int, bytes] = {}
            for item in _arr(_get(_obj(data), "messages")):
                obj = _obj(item)
                uid, body = _get(obj, "uid"), _get(obj, "body")
                if uid is not None and isinstance(body, str):
                    found[_int(uid)] = base64.b64decode(body)
            return found
        except (_Shape, ValueError):  # ValueError: not base64
            _bad(data)

    def raw(self, mailbox: str, folder: str, id: str) -> bytes:
        """The exact bytes of one mail; reading never marks it as read."""
        out = self.run(mailbox, "message", "read", str(id), "-m", folder, "--raw", json=False)
        if not out:
            raise HimalayaError(f"himalaya returned no message for {id} in {folder}")
        return out

    def send(self, mailbox: str, raw: bytes) -> None:
        """Send a mail without saving a copy (the caller appends the copy: see `add`). himalaya
        stops reading at the first line that is not UTF-8, so the mail must be 7-bit."""
        _check_mail(raw)
        self.run(mailbox, "message", "send", "--no-save", input=raw)

    def add(self, mailbox: str, folder: str, raw: bytes) -> None:
        """Append a mail to `folder`, marked as read."""
        _check_mail(raw)
        self.run(mailbox, "message", "add", "-m", folder, "-f", "seen", input=raw)

    def mark_seen(self, mailbox: str, folder: str, id: str) -> None:
        self.run(mailbox, "flag", "add", "-m", folder, str(id), "-f", "seen")

    def move(self, mailbox: str, folder: str, id: str, target: str) -> None:
        """Move a mail to `target`. himalaya answers exit 0 when nothing moved, so the text is
        checked."""
        data = self._answer(
            mailbox, "message", "move", str(id), "--from", folder, "--to", target)
        answer = _get(data, "message") if isinstance(data, dict) else None
        if not isinstance(answer, str):
            _bad(data)
        if answer.startswith("No message moved"):
            raise HimalayaError(answer[:300])

    def check(self, mailbox: str) -> list[tuple[str, bool, str | None]]:
        """Open each backend of the account: `(backend, ok, error)`. A failing backend is a
        result here, not an exception."""
        data = self._answer(mailbox, "account", "check")
        try:
            found = []
            for item in _arr(_get(_obj(data), "backends")):
                obj = _obj(item)
                backend, ok, error = _get(obj, "backend"), _get(obj, "ok"), _get(obj, "error")
                if not isinstance(backend, str) or not isinstance(ok, bool):
                    raise _Shape
                found.append((backend, ok, error if isinstance(error, str) else None))
            return found
        except _Shape:
            _bad(data)


def _check_mail(raw: bytes) -> None:
    if not raw:
        raise MailError("the mail is empty: nothing was sent to himalaya")
    if not raw.isascii():
        raise MailError(
            "the mail is not 7-bit: himalaya would cut it at the first line that is not UTF-8")

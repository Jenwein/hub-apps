"""Drafts of outgoing mail and the one-time codes that let them go out.

No mail leaves without the user's code. Every command that makes or changes a draft gives it a
new code, which goes with the whole draft to the user's private chat and nowhere else: not to
stdout, not to the state. The state keeps a salted scrypt hash of the code, made over the draft's
id, the code, the digest of the draft's content (`content_digest`, attachment bytes included) and
the time the code was issued. So a change of the draft or of an attached file voids the code, and
so does an edit of those fields in the draft's file: the code no longer matches. A code is good
for one send, for CODE_TTL, and for MAX_ATTEMPTS wrong tries.

A draft is `state/drafts/<id>.json`. A sent draft stays, as the record that its code was used.
Changes to drafts are made under `lock(ctx)`, the lock of the drafts folder.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from collections.abc import Iterator
from dataclasses import MISSING, asdict, dataclass, fields
from datetime import datetime, timedelta
from pathlib import Path

from .context import Ctx
from .paths import POSIX, locked, read_json, write_json
from .settings import MailError

CODE_DIGITS = 6
CODE_TTL = timedelta(hours=24)
MAX_ATTEMPTS = 3
MAX_ATTACH_BYTES = 20 * 1024 * 1024

DRAFTS_DIR = "drafts"  # in the state directory
ID = re.compile(r"d[0-9a-f]{6}")
SCRYPT = {"n": 2**14, "r": 8, "p": 1}
# What the digest covers, besides the attachments: everything that decides the mail sent.
CONTENT = (
    "mailbox", "to", "cc", "bcc", "subject", "body", "in_reply_to", "references", "forward_key")
LISTS = frozenset({"to", "cc", "bcc", "attachments"})

# An address as the commands take it: ASCII, one @, no white space, a dot in the domain, and none
# of the characters that would make it more than one address (`,`, `;`, `<`, `"`, ...).
LOCAL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}")
DOMAIN = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z0-9-]*[A-Za-z0-9]")
ADDRESS_MAX = 254


@dataclass
class Draft:
    id: str
    mailbox: str
    to: list[str]
    cc: list[str]
    bcc: list[str]
    subject: str
    body: str
    attachments: list[str]  # absolute paths of the files
    in_reply_to: str
    references: str
    forward_key: str  # <mailbox>/<role>/<id> of the mail a forward attaches, else ""
    written_by: str  # "agent" | "reader"
    created_at: str
    digest: str = ""
    code_hash: str = ""
    code_salt: str = ""
    code_issued_at: str = ""
    attempts: int = 0
    sent_at: str = ""
    message_id: str = ""
    reply_key: str = ""  # <mailbox>/<role>/<id> of the mail a reply answers, for `revise`
    intent: str = ""  # what the user wants a reply to say, for `revise`


class CodeError(MailError):
    """The code does not let the draft go: wrong, void, expired, spent or never issued."""


# --- addresses ---------------------------------------------------------------------------------


def valid_address(text: str) -> bool:
    """Is `text` one plain address, such as `bob@example.org`? One `@`, no white space, a dot in
    the domain; ASCII only, and no character that would make it a name or a list."""
    if not text or len(text) > ADDRESS_MAX or not text.isascii():
        return False
    local, at, domain = text.partition("@")
    return bool(at) and bool(LOCAL.fullmatch(local)) and bool(DOMAIN.fullmatch(domain))


# --- the content and its digest ----------------------------------------------------------------


def read_files(d: Draft) -> list[tuple[str, bytes]]:
    """`(path, bytes)` of each attachment, in order."""
    found = []
    for path in d.attachments:
        try:
            found.append((path, Path(path).read_bytes()))
        except OSError as err:
            raise MailError(
                f"cannot read the attachment {path}: {err.strerror or err}") from None
    return found


def content_digest(d: Draft, files: list[tuple[str, bytes]] | None = None) -> str:
    """SHA-256 of what the mail would be: the CONTENT fields and, for each attachment, its path
    and the SHA-256 of its bytes (`files`, as `read_files` gives them; read now when None)."""
    if files is None:
        files = read_files(d)
    data: dict[str, object] = {name: getattr(d, name) for name in CONTENT}
    data["attachments"] = [[path, hashlib.sha256(blob).hexdigest()] for path, blob in files]
    text = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- codes -------------------------------------------------------------------------------------


def _hash(d: Draft, code: str, salt: bytes) -> str:
    """The scrypt hash of `code`, bound to the draft, its digest and the time the code was issued:
    a change of any of them in the file makes the code wrong."""
    material = json.dumps([d.id, code, d.digest, d.code_issued_at]).encode("utf-8")
    return hashlib.scrypt(material, salt=salt, **SCRYPT).hex()


def issue_code(d: Draft, now: datetime, files: list[tuple[str, bytes]] | None = None) -> str:
    """Give `d` a new code and return it; only its hash is kept. Any code before it is void."""
    code = f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"
    salt = secrets.token_bytes(16)
    d.digest = content_digest(d, files)
    d.code_issued_at = now.isoformat()
    d.code_salt = salt.hex()
    d.code_hash = _hash(d, code, salt)
    d.attempts = 0
    return code


def void(d: Draft) -> None:
    """Forget the code of `d`: no code is waiting any more."""
    d.code_hash = d.code_salt = d.code_issued_at = ""


def _issued(d: Draft) -> datetime | None:
    try:
        issued = datetime.fromisoformat(d.code_issued_at)
    except ValueError:
        return None
    return issued if issued.tzinfo else None


def _expired(d: Draft, now: datetime) -> bool:
    issued = _issued(d)
    return issued is None or now - issued > CODE_TTL


def waiting(d: Draft, now: datetime) -> bool:
    """Does a code wait for `d`, one that has not expired?"""
    return bool(d.code_hash) and not d.sent_at and not _expired(d, now)


def _typed(code: str) -> str:
    """The code as the user may have typed it: full-width digits and spaces are fine."""
    return "".join(unicodedata.normalize("NFKC", code).split())


def check_code(d: Draft, code: str, now: datetime,
               files: list[tuple[str, bytes]] | None = None) -> None:
    """Does `code` let `d` go now? A CodeError says why not. A wrong code counts as a try, and
    the MAX_ATTEMPTS-th voids the code; the caller saves `d` after a CodeError."""
    if d.sent_at:
        raise CodeError("already sent")
    if not d.code_hash:
        raise CodeError(f"no code is waiting: run mx confirm {d.id}")
    if content_digest(d, files) != d.digest:
        raise CodeError(f"the draft changed after its code was sent: run mx confirm {d.id}")
    if _expired(d, now):
        raise CodeError(f"the code expired: run mx confirm {d.id}")
    given = _typed(code)
    try:
        salt = bytes.fromhex(d.code_salt)
    except ValueError:
        salt = b""
    if (len(given) == CODE_DIGITS and given.isascii() and given.isdigit() and salt
            and hmac.compare_digest(_hash(d, given, salt), d.code_hash)):
        return
    d.attempts += 1
    if d.attempts >= MAX_ATTEMPTS:
        void(d)
        raise CodeError(
            f"wrong code {MAX_ATTEMPTS} times: the code is void; run mx confirm {d.id}")
    raise CodeError(f"wrong code ({MAX_ATTEMPTS - d.attempts} tries left)")


# --- the drafts folder -------------------------------------------------------------------------


def new_id() -> str:
    return "d" + secrets.token_hex(3)


def folder(ctx: Ctx) -> Path:
    path = ctx.state / DRAFTS_DIR
    try:
        path.mkdir(exist_ok=True, mode=0o700)
        if POSIX:
            path.chmod(0o700)
    except OSError as err:
        raise MailError(f"cannot create {path}: {err}") from err
    return path


@contextlib.contextmanager
def lock(ctx: Ctx) -> Iterator[None]:
    """One writer of drafts at a time. Its own lock, so that a send waiting on the server does
    not hold up the rest of the app."""
    with locked(folder(ctx)):
        yield


def _path(ctx: Ctx, id: str) -> Path:
    if not ID.fullmatch(id):
        raise MailError(f"no draft {id}")
    return folder(ctx) / f"{id}.json"


def _draft(id: str, data: object) -> Draft:
    """The draft in `data`, as `save` wrote it; a MailError if it is not one."""
    damaged = MailError(f"draft {id} is damaged: discard it with mx discard {id}")
    if not isinstance(data, dict) or data.get("id") != id:
        raise damaged
    values = {}
    for spec in fields(Draft):
        if spec.name not in data:
            if spec.default is MISSING:
                raise damaged
            continue
        value = data[spec.name]
        if spec.name in LISTS:
            good = isinstance(value, list) and all(isinstance(item, str) for item in value)
        elif spec.name == "attempts":
            good = isinstance(value, int) and not isinstance(value, bool)
        else:
            good = isinstance(value, str)
        if not good:
            raise damaged
        values[spec.name] = value
    return Draft(**values)


def save(ctx: Ctx, d: Draft) -> None:
    write_json(_path(ctx, d.id), asdict(d))


def load(ctx: Ctx, id: str) -> Draft:
    data = read_json(_path(ctx, id), None)
    if data is None:
        raise MailError(f"no draft {id}")
    return _draft(id, data)


def unused_id(ctx: Ctx) -> str:
    """A new id that no draft has (call it under `lock`)."""
    while True:
        id = new_id()
        if not _path(ctx, id).exists():
            return id


def pending(ctx: Ctx) -> list[Draft]:
    """The drafts not sent yet, oldest first. A damaged file is left out."""
    found = []
    for path in folder(ctx).glob("d*.json"):
        if not ID.fullmatch(path.stem):
            continue
        try:
            d = load(ctx, path.stem)
        except MailError:
            continue
        if not d.sent_at:
            found.append(d)
    return sorted(found, key=lambda d: (d.created_at, d.id))


def discard(ctx: Ctx, id: str) -> None:
    path = _path(ctx, id)
    try:
        path.unlink()
    except FileNotFoundError:
        raise MailError(f"no draft {id}") from None
    except OSError as err:
        raise MailError(f"cannot discard draft {id}: {err}") from err

"""Drafts: writing new mail, replies and forwards, and sending them.

The fixed rule (spec §4.1): mail goes out only as a saved draft that was shown to the user in
full and has not changed since. The tool holds to it, so that no slip of the agent can break it:

- `create` saves a draft in `mail/.state/drafts/<draft id>.json`: a new mail, a reply to a
  message of the index (`reply_to`), or a forward of one with its attachments (`forward_of`).
  Attachment files are read when they are added. `edit` changes a draft.
- `show` renders the whole draft as text: the mailbox and address it goes out from, To, Cc,
  Subject, each attachment with its size, the total size with the provider's limit and a
  WARNING line when the mail is over it, then the text that is sent. It reads every attachment
  file again and records a hash of everything it showed (`shown_hash`), the content of each
  attachment included.
- `send` refuses a draft that was never shown, one whose hash is not the one shown (an edit
  since, or an attachment file that changed on disk: the files are read again, and what is sent
  is exactly the bytes that were checked), and one over the size limit. Then it builds the
  message, sends it, deletes the draft and saves a copy in Sent unless the provider does that
  itself.

Replies: from the mailbox that received the original (unless another is named), to its Reply-To
addresses, else to its sender; `reply_all` adds its To and Cc. A message the user sent
themselves is answered to its recipients. The user's own addresses (every mailbox in
config.toml, whatever the case of the letters or a `+tag`) are never among the recipients
taken from the original, and no address is there twice. The subject gets one `Re: `;
In-Reply-To and References keep the thread.

Forwards: `Fwd: ` once, the original's attachments (downloaded with BODY.PEEK, so it stays
unread), and the original quoted below the user's text.

A failed send keeps the draft, and the error says whether the mail may have gone out: when the
login failed or the server refused the send, nothing was sent; when the connection broke during
the send, the mail may have been sent. A password is never in a draft, a result or an error.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import mimetypes
import os
import re
import secrets
import stat
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, make_msgid
from pathlib import Path

from . import accounts, config, server
from .actions import (
    _check_id,
    _mailbox_of,
    _named,
    _scrub,
    _special,
    download_attachment,
    safe_filename,
)
from .config import Mailbox, MailError, Settings
from .providers import PROVIDERS
from .server import LoginError, MailServer, SendRefused, ServerError
from .store import STATE_DIR, Store

DRAFTS_DIR = "drafts"  # in mail/.state/
DRAFT_ID_LENGTH = 8
DRAFT_ID_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"
FORMAT = 1  # of a draft file
MB = 1024 * 1024
MAX_REFERENCES = 20  # ids kept in References: the first of the thread and the latest ones
SENT = "\\Sent"
KINDS = ("new", "reply", "forward")
EDITABLE = ("mailbox", "to", "cc", "subject", "body", "attach", "drop")
# CRLF line ends, and 7-bit: a header with non-ASCII text is RFC 2047, a file name RFC 2231, the
# text base64 or quoted-printable, so no server needs 8BITMIME
POLICY = SMTP.clone(cte_type="7bit")
# what the hash of a draft covers, besides its attachments (name, type, size and content)
_HASHED = ("kind", "source", "mailbox", "address", "provider", "to", "cc", "subject", "body",
           "quoted", "in_reply_to", "references")

# one plain address: no display name, no list, nothing a header could be split at
_ADDRESS = re.compile(r'[^\s@,;:<>()\[\]"\\\x00-\x1f\x7f]+@[^\s@,;:<>()\[\]"\\\x00-\x1f\x7f]+')
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_REPLY = re.compile(r"\s*(re|回复|答复|回覆)\s*(\[\d+\])?\s*[:：]", re.IGNORECASE)
_FORWARD = re.compile(r"\s*(fwd?|转发|轉發|转寄|轉寄)\s*(\[\d+\])?\s*[:：]", re.IGNORECASE)
_CONTENT_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*", re.IGNORECASE)


# -- values ----------------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _size(count: int) -> str:
    if count < 1024:
        return f"{count} byte{'' if count == 1 else 's'}"
    if count < MB:
        return f"{count / 1024:.1f} KB"
    return f"{count / MB:.1f} MB"


def _usable(address) -> bool:
    return (isinstance(address, str) and address.strip().isascii()
            and _ADDRESS.fullmatch(address.strip()) is not None)


def _address(value, field: str) -> str:
    if not _usable(value):
        raise MailError(
            f"{field}: {value!r} is not an email address (give one plain address each time, "
            "such as name@example.com)")
    return value.strip()


def _addresses(values, field: str) -> list[str]:
    if isinstance(values, str):
        values = [values]
    return [_address(value, field) for value in values or ()]


def _untagged(address: str) -> str:
    """`address` without a `+tag` in its local part."""
    local, at, domain = address.strip().rpartition("@")
    if not at:
        return address.strip()
    return f"{local.partition('+')[0] or local}@{domain}"


def _mine(address: str, own: list[str]) -> bool:
    """Whether `address` is one of the user's: case and a `+tag` do not matter."""
    return any(accounts.same_address(_untagged(address), _untagged(mine)) for mine in own)


def _unique(addresses: list[str], besides: list[str] = ()) -> list[str]:
    """`addresses` in order, each once, without those in `besides` (case does not matter)."""
    kept: list[str] = []
    for address in addresses:
        if not any(accounts.same_address(address, other) for other in (*besides, *kept)):
            kept.append(address)
    return kept


def _one_line(value) -> bool:
    return isinstance(value, str) and not _CONTROL.search(value) and len(
        value.splitlines()) <= 1


def _subject(value) -> str:
    if not _one_line(value):
        raise MailError("the subject must be one line of text, without control characters")
    return value.strip()


def _clean_body(text) -> str:
    """The text of a mail: no byte order mark, `\\n` line ends, no trailing blank lines."""
    if not isinstance(text, str):
        raise MailError("the text of a mail must be text")
    text = text.removeprefix("﻿").replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip()


def reply_subject(subject: str) -> str:
    """`Re: <subject>`, unless the subject already starts as a reply (`Re:`, `RE:`, `回复:`,
    `Re：` ...): replies to replies do not stack prefixes."""
    subject = subject.strip()
    return subject if _REPLY.match(subject) else f"Re: {subject}".rstrip()


def forward_subject(subject: str) -> str:
    """`Fwd: <subject>`, unless the subject already starts as a forward (`Fwd:`, `FW:`,
    `转发:` ...)."""
    subject = subject.strip()
    return subject if _FORWARD.match(subject) else f"Fwd: {subject}".rstrip()


# -- draft files -----------------------------------------------------------------------------


def _check_draft_id(draft_id) -> str:
    if not (isinstance(draft_id, str) and len(draft_id) == DRAFT_ID_LENGTH
            and all(char in DRAFT_ID_CHARS for char in draft_id)):
        raise MailError(
            f"{draft_id!r} is not a draft id (draft new, reply and forward print one: "
            f"{DRAFT_ID_LENGTH} lower-case letters and digits)")
    return draft_id


def _path(mail_dir: Path, draft_id: str) -> Path:
    return Path(mail_dir) / STATE_DIR / DRAFTS_DIR / f"{_check_draft_id(draft_id)}.json"


def _valid_attachment(entry) -> bool:
    return (isinstance(entry, dict)
            and all(isinstance(entry.get(key), str) for key in ("path", "name", "sha256"))
            and _one_line(entry["name"])
            and isinstance(entry.get("content_type"), str)
            and _CONTENT_TYPE.fullmatch(entry["content_type"]) is not None
            and isinstance(entry.get("size"), int) and not isinstance(entry["size"], bool))


def _valid(data) -> bool:
    """Whether `data` is a draft that can be shown and sent as it is (a hand-edited file could
    hold anything, a header split by a line break included)."""
    if not isinstance(data, dict) or data.get("kind") not in KINDS:
        return False
    texts = ("mailbox", "provider", "body", "quoted")
    lists = ("to", "cc", "references")
    source = data.get("source")
    return (all(isinstance(data.get(key), str) for key in texts)
            and _one_line(data["mailbox"]) and _usable(data.get("address"))
            and _one_line(data.get("subject"))
            and all(isinstance(data.get(key), list) for key in lists)
            and all(_usable(address) for key in ("to", "cc") for address in data[key])
            and all(_one_line(ref) and " " not in ref for ref in data["references"])
            and (data.get("in_reply_to") is None or (
                _one_line(data["in_reply_to"]) and " " not in data["in_reply_to"]))
            and (source is None or (isinstance(source, int) and not isinstance(source, bool)))
            and (data.get("shown_hash") is None or isinstance(data["shown_hash"], str))
            and isinstance(data.get("attachments"), list)
            and all(_valid_attachment(entry) for entry in data["attachments"]))


def read(mail_dir: Path, draft_id: str) -> dict:
    """The draft `draft_id` as it is stored. MailError when there is none (never made, or sent)
    or it cannot be used."""
    path = _path(mail_dir, draft_id)
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        raise MailError(
            f"no draft {draft_id}: it was never made, or it was sent already") from None
    except (OSError, ValueError) as err:
        raise MailError(f"the draft {draft_id} cannot be read ({err}); make a new one") from None
    if not _valid(data):
        raise MailError(f"the draft {draft_id} cannot be read (it is not a draft); make a new one")
    if data.get("sent_at"):
        raise MailError(f"draft {draft_id} was sent at {data['sent_at']}; make a new draft")
    return data


def _write(mail_dir: Path, draft_id: str, draft: dict) -> None:
    path = _path(mail_dir, draft_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        config.atomic_write(
            path, json.dumps(draft, ensure_ascii=False, indent=1).encode("utf-8"), mode=0o600)
    except OSError as err:
        raise MailError(f"cannot save the draft in {path.parent}: {err.strerror or err}") from None


def _new_id(mail_dir: Path) -> str:
    for _ in range(100):
        draft_id = "".join(secrets.choice(DRAFT_ID_CHARS) for _ in range(DRAFT_ID_LENGTH))
        if not _path(mail_dir, draft_id).exists():
            return draft_id
    raise MailError("cannot find a free draft id")  # (36^8 of them)


# -- attachments -----------------------------------------------------------------------------


def _read_file(path: Path) -> bytes:
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            raise MailError(f"the attachment {path} is not a file")
        return path.read_bytes()
    except FileNotFoundError:
        raise MailError(f"the attachment {path} does not exist") from None
    except OSError as err:
        raise MailError(f"cannot read the attachment {path}: {err.strerror or err}") from None


def _content_type(name: str, given: str | None) -> str:
    """The MIME type an attachment is sent as. A message or multipart type would need its own
    encoding rules: such a file goes as plain bytes."""
    kind = given if given and _CONTENT_TYPE.fullmatch(given) else mimetypes.guess_type(name)[0]
    if not kind or not _CONTENT_TYPE.fullmatch(kind) or kind.lower().startswith(
            ("message/", "multipart/")):
        return "application/octet-stream"
    return kind.lower()


def _attachment(path, *, name: str | None = None, content_type: str | None = None,
                index: int = 0) -> dict:
    """The draft entry of the file `path`, read now: where it is, the name it is sent under
    (`name`, default the file's own), its type, size and content hash."""
    path = Path(os.path.abspath(path))
    data = _read_file(path)
    name = safe_filename(path.name if name is None else name, index)
    return {"path": str(path), "name": name, "content_type": _content_type(name, content_type),
            "size": len(data), "sha256": _sha256(data)}


# -- what the user sees ----------------------------------------------------------------------


def _text(draft: dict) -> str:
    """The text that is sent: the user's, then the quoted original of a forward."""
    return "\n\n".join(part for part in (draft["body"], draft["quoted"]) if part)


def _total(draft: dict) -> int:
    return len(_text(draft).encode("utf-8")) + sum(a["size"] for a in draft["attachments"])


def _too_large(draft_id: str, draft: dict) -> str | None:
    """Why the provider would refuse the mail for its size, or None."""
    provider = PROVIDERS.get(draft["provider"])
    total = _total(draft)
    if provider is None or total <= provider.max_message_mb * MB:
        return None
    return (f"this mail is {_size(total)}, more than the {provider.max_message_mb} MB "
            f"{provider.name} takes, so it cannot be sent as it is; drop or shrink attachments "
            f"(mk draft edit {draft_id} --drop N)")


def _digest(draft: dict) -> str:
    """The hash of everything `show` shows, and what is sent with it."""
    shown = {key: draft[key] for key in _HASHED}
    shown["attachments"] = [
        {key: entry[key] for key in ("name", "content_type", "size", "sha256")}
        for entry in draft["attachments"]]
    text = json.dumps(shown, ensure_ascii=False, sort_keys=True)
    return _sha256(text.encode("utf-8", "surrogatepass"))


def _render(draft_id: str, draft: dict) -> str:
    kind = {
        "new": "a new mail",
        "reply": f"a reply to message {draft['source']}",
        "forward": f"a forward of message {draft['source']}",
    }[draft["kind"]]
    lines = [
        f"Draft {draft_id} ({kind}), not sent yet",
        f"From: {draft['mailbox']} <{draft['address']}>",
        f"To: {', '.join(draft['to'])}",
        f"Cc: {', '.join(draft['cc']) or '(none)'}",
        f"Subject: {draft['subject'] or '(none)'}",
    ]
    attachments = draft["attachments"]
    if attachments:
        lines.append(f"Attachments: {len(attachments)}")
        lines += [f"  {number}. {entry['name']} ({_size(entry['size'])})"
                  for number, entry in enumerate(attachments, 1)]
    else:
        lines.append("Attachments: none")
    provider = PROVIDERS.get(draft["provider"])
    size = f"Size: {_size(_total(draft))} in all"
    if provider is not None:
        size += f" ({provider.name} takes up to {provider.max_message_mb} MB)"
    lines.append(size)
    warning = _too_large(draft_id, draft)
    if warning:
        lines.append(f"WARNING: {warning}")
    text = _text(draft)
    count = len(text.split("\n")) if text else 0
    lines.append(f"--- text: {count} line{'' if count == 1 else 's'} ---")
    if text:
        lines.append(text)
    lines.append("--- end of text ---")
    return "\n".join(lines)


# -- making a draft --------------------------------------------------------------------------


def _pick(settings: Settings, name: str | None) -> Mailbox:
    """The mailbox to send from: `name` (a short name, or an address); with no name the only
    mailbox there is, else MailError "which mailbox"."""
    boxes = settings.mailboxes
    if name is None:
        if len(boxes) == 1:
            return next(iter(boxes.values()))
        if not boxes:
            raise MailError("no mailbox is set up yet: add one first")
        raise MailError(
            f"which mailbox: {', '.join(boxes)}? Ask the user, then give it with --from NAME")
    if name in boxes:
        return boxes[name]
    for mb in boxes.values():
        if accounts.same_address(mb.address, name):
            return mb
    raise MailError(f"no mailbox named {name!r}; the mailboxes are: {', '.join(boxes) or 'none'}")


def _original(mail_dir: Path, mid: int) -> dict:
    _check_id(mid)
    with Store.open(mail_dir) as store:
        return store.message(mid)


def _receiving(settings: Settings, original: dict) -> Mailbox:
    mb = _mailbox_of(settings, original["address"])
    if mb is None:
        raise MailError(
            f"message {original['id']} came to {original['address']}, which is not in "
            "config.toml any more; name the mailbox to send from (--from NAME)")
    return mb


def _reply_recipients(original: dict, own: list[str], reply_all: bool):
    """(to, cc, warnings) of a reply to `original`; `own` are the user's addresses."""
    warnings: list[str] = []

    def others(addresses, header):
        kept = []
        for address in addresses:
            if not _usable(address):
                warnings.append(
                    f"left out {address!r} from the {header} of the original: it is not an "
                    "address mail can be sent to")
            elif not _mine(address, own):
                kept.append(address.strip())
        return kept

    sender = original["from_addr"]
    if sender and _mine(sender, own):  # the user's own mail: answer its recipients
        # (sent to nobody but the user: answer the user)
        to = others(original["to"], "To") or ([sender.strip()] if _usable(sender) else [])
    else:
        to = others(original["reply_to"], "Reply-To")
        if to and not (sender and all(accounts.same_address(a, sender) for a in to)):
            warnings.append(
                f"the reply goes to {', '.join(to)}, the Reply-To of the original, not to its "
                f"sender {sender or '(none)'}: tell the user")
        to = to or (others([sender], "From") if sender else [])
        if to and reply_all:
            to += others(original["to"], "To")
    if not to:
        raise MailError(
            f"message {original['id']} has no sender address to reply to; write a new mail "
            "to the right address instead")
    cc = others(original["cc"], "Cc") if reply_all else []
    return to, cc, warnings


def _references(original: dict) -> list[str]:
    """References of a reply: the original's (or its In-Reply-To), then its Message-ID; a long
    thread keeps its first id and the latest ones."""
    refs = list(original["references"]) or (
        [original["in_reply_to"]] if original["in_reply_to"] else [])
    if original["message_id"] and original["message_id"] not in refs:
        refs.append(original["message_id"])
    if len(refs) > MAX_REFERENCES:
        refs = [refs[0], *refs[-(MAX_REFERENCES - 1):]]
    return refs


def _quote(original: dict) -> str:
    """The original of a forward, as it goes below the user's text."""
    name, sender = original["from_name"], original["from_addr"]
    lines = ["---------- Forwarded message ----------",
             f"From: {name} <{sender}>" if name else f"From: {sender}"]
    when = original["date_original"] or original["date"]
    if when:
        lines.append(f"Date: {when.replace('T', ' ')}")
    lines.append(f"Subject: {original['subject']}")
    if original["to"]:
        lines.append(f"To: {', '.join(original['to'])}")
    if original["cc"]:
        lines.append(f"Cc: {', '.join(original['cc'])}")
    head = "\n".join(lines)
    text = _clean_body(original["text"])
    return f"{head}\n\n{text}" if text else head


def _forwarded(hub: Path, mail_dir: Path, original: dict, connect) -> list[dict]:
    """The original's attachments, downloaded (or found downloaded already), as draft
    entries under their own names."""
    entries = []
    for item in original["attachments"]:
        try:
            path = download_attachment(hub, mail_dir, original["id"], item["index"],
                                       connect=connect)
        except MailError as err:
            raise MailError(
                f"cannot forward message {original['id']} with its attachments: {err}") from None
        entries.append(_attachment(path, name=item["filename"],
                                   content_type=item["content_type"], index=item["index"]))
    return entries


def create(hub: Path, mail_dir: Path, *, mailbox: str | None, to: list[str], cc: list[str],
           subject: str | None, body: str, attach: list[Path], reply_to: int | None = None,
           reply_all: bool = False, forward_of: int | None = None, connect=None) -> dict:
    """Save a draft and return `{"draft_id", "mailbox", "warnings"}`. Nothing is sent.

    - A new mail: `mailbox` (needed when there is more than one), `to` (at least one),
      `subject`.
    - `reply_to=<message id>`: from the mailbox that received it (or `mailbox`), to its
      Reply-To or sender (`reply_all`: and its To and Cc), never to the user's own addresses;
      `to` and `cc` add recipients; `subject` replaces `Re: <its subject>`.
    - `forward_of=<message id>`: to `to` (at least one), with its attachments and its text
      quoted below `body`; `subject` replaces `Fwd: <its subject>`.

    `attach` are files, read now; `warnings` says what was left out of a reply and whether the
    mail is too large for the provider."""
    mail_dir, hub = Path(mail_dir), Path(hub)
    if reply_to is not None and forward_of is not None:
        raise MailError("a draft is a reply or a forward, not both")
    if reply_all and reply_to is None:
        raise MailError("reply to all (--all) goes with a reply")
    to, cc = _addresses(to, "To"), _addresses(cc, "Cc")
    subject = None if subject is None else _subject(subject)
    body = _clean_body(body)
    settings = config.load(mail_dir)
    files = [_attachment(path, index=number) for number, path in enumerate(attach or ())]
    draft = {"format": FORMAT, "kind": "new", "source": None, "created_at": _now(),
             "in_reply_to": None, "references": [], "quoted": "", "shown_hash": None,
             "shown_at": None}
    warnings: list[str] = []
    attachments: list[dict] = []
    if reply_to is None and forward_of is None:
        mb = _pick(settings, mailbox)
        if not to:
            raise MailError("a new mail needs at least one To address")
        if subject is None:
            raise MailError("a new mail needs a subject")
    else:
        source = reply_to if reply_to is not None else forward_of
        original = _original(mail_dir, source)
        mb = _pick(settings, mailbox) if mailbox is not None else _receiving(settings, original)
        draft["source"] = source
        if reply_to is not None:
            own = [box.address for box in settings.mailboxes.values()]
            first, copies, warnings = _reply_recipients(original, own, reply_all)
            to, cc = [*first, *to], [*copies, *cc]
            draft.update(kind="reply", in_reply_to=original["message_id"],
                         references=_references(original))
            subject = reply_subject(original["subject"]) if subject is None else subject
        else:
            if not to:
                raise MailError("a forward needs at least one To address")
            draft.update(kind="forward", quoted=_quote(original))
            subject = forward_subject(original["subject"]) if subject is None else subject
            attachments = _forwarded(hub, mail_dir, original, connect)
    to = _unique(to)
    draft.update(mailbox=mb.name, address=mb.address, provider=mb.provider, to=to,
                 cc=_unique(cc, besides=to), subject=subject, body=body,
                 attachments=[*attachments, *files])
    draft_id = _new_id(mail_dir)
    _write(mail_dir, draft_id, draft)
    warning = _too_large(draft_id, draft)
    return {"draft_id": draft_id, "mailbox": mb.name,
            "warnings": [*warnings, *([warning] if warning else [])]}


def edit(mail_dir: Path, draft_id: str, **changes) -> dict:
    """Change a draft: `mailbox` (send from another), `to` and `cc` (replace the lists),
    `subject`, `body`, `attach` (files to add) and `drop` (attachment numbers to remove, as
    `show` numbers them, from 1). A change needs a new `show` before `send`. Returns
    `{"draft_id", "mailbox", "changed", "warnings"}`."""
    mail_dir = Path(mail_dir)
    _check_draft_id(draft_id)
    unknown = sorted(set(changes) - set(EDITABLE))
    if unknown:
        raise MailError(
            f"cannot change {', '.join(unknown)} of a draft; what can change: "
            f"{', '.join(EDITABLE)}")
    changes = {key: value for key, value in changes.items() if value is not None}
    if not changes:
        raise MailError(
            "nothing to change: give a mailbox, To, Cc, a subject, a text, files to attach or "
            "attachments to drop")
    draft = read(mail_dir, draft_id)
    if "mailbox" in changes:
        mb = _pick(config.load(mail_dir), changes["mailbox"])
        draft.update(mailbox=mb.name, address=mb.address, provider=mb.provider)
    if "to" in changes:
        draft["to"] = _addresses(changes["to"], "To")
        if not draft["to"]:
            raise MailError("a mail needs at least one To address")
    if "cc" in changes:
        draft["cc"] = _addresses(changes["cc"], "Cc")
    if "subject" in changes:
        draft["subject"] = _subject(changes["subject"])
    if "body" in changes:
        draft["body"] = _clean_body(changes["body"])
    if "drop" in changes:
        count = len(draft["attachments"])
        for number in changes["drop"]:
            if isinstance(number, bool) or not isinstance(number, int) or not (
                    1 <= number <= count):
                raise MailError(
                    f"the draft has no attachment {number} (it has {count}, numbered from 1 "
                    "as draft show lists them)")
        draft["attachments"] = [entry for number, entry in enumerate(draft["attachments"], 1)
                                if number not in changes["drop"]]
    if "attach" in changes:
        start = len(draft["attachments"])
        draft["attachments"] += [_attachment(path, index=start + number)
                                 for number, path in enumerate(changes["attach"])]
    draft["to"] = _unique(draft["to"])
    draft["cc"] = _unique(draft["cc"], besides=draft["to"])
    _write(mail_dir, draft_id, draft)
    warning = _too_large(draft_id, draft)
    return {"draft_id": draft_id, "mailbox": draft["mailbox"], "changed": sorted(changes),
            "warnings": [warning] if warning else []}


# -- showing and sending ---------------------------------------------------------------------


def show(mail_dir: Path, draft_id: str) -> str:
    """The whole draft as the text to show the user, and the record that it was shown: the
    attachment files are read again (their size and content as they are now are what is shown
    and what `send` checks), and the hash of it all is kept as `shown_hash`."""
    mail_dir = Path(mail_dir)
    draft = read(mail_dir, draft_id)
    for number, entry in enumerate(draft["attachments"], 1):
        try:
            data = _read_file(Path(entry["path"]))
        except MailError as err:
            raise MailError(
                f"attachment {number} of draft {draft_id} ({entry['name']}) cannot be read: "
                f"{err}; drop it (mk draft edit {draft_id} --drop {number}) or attach the file "
                "again") from None
        entry.update(size=len(data), sha256=_sha256(data))
    draft["shown_hash"] = _digest(draft)
    draft["shown_at"] = _now()
    _write(mail_dir, draft_id, draft)
    return _render(draft_id, draft)


def _build(draft: dict, contents: list[bytes]) -> tuple[bytes, str]:
    """The message of `draft` with the attachment `contents`, and its Message-ID."""
    sender = draft["address"]
    msg = EmailMessage(policy=POLICY)
    msg["From"] = sender
    msg["To"] = ", ".join(draft["to"])
    if draft["cc"]:
        msg["Cc"] = ", ".join(draft["cc"])
    msg["Subject"] = draft["subject"]
    msg["Date"] = formatdate(localtime=True)
    message_id = make_msgid(domain=sender.rpartition("@")[2])
    msg["Message-ID"] = message_id
    if draft["in_reply_to"]:
        msg["In-Reply-To"] = draft["in_reply_to"]
    if draft["references"]:
        msg["References"] = " ".join(draft["references"])
    msg.set_content(_text(draft))
    for entry, data in zip(draft["attachments"], contents, strict=True):
        maintype, subtype = entry["content_type"].split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=entry["name"])
    return msg.as_bytes(), message_id


def _not_sent(draft_id: str, detail: str, password: str) -> MailError:
    return MailError(
        f"not sent: {_scrub(detail, password)}. Draft {draft_id} is kept and can be sent once "
        "the problem is fixed")


def _maybe_sent(draft_id: str, mb: Mailbox, detail: str, password: str) -> MailError:
    provider = PROVIDERS.get(mb.provider)
    if provider is not None and provider.saves_sent is True:
        check = f"the Sent folder of {mb.name} holds it if it went out"
    else:
        check = (f"the server of {mb.name} may not keep a copy of mail sent this way, so it can "
                 "have gone out without showing there; ask the user whether to send it again "
                 "(it may arrive twice)")
    return MailError(
        f"the mail may have been sent: {_scrub(detail, password)}. Check Sent before retrying: "
        f"{check}. Draft {draft_id} is kept")


def _forget(mail_dir: Path, draft_id: str, draft: dict) -> list[str]:
    """Delete a sent draft; a file that cannot be deleted is marked sent, so it is never sent
    twice."""
    path = _path(mail_dir, draft_id)
    try:
        path.unlink()
        return []
    except FileNotFoundError:
        return []
    except OSError as err:
        why = err.strerror or str(err)
    try:
        _write(mail_dir, draft_id, {**draft, "sent_at": _now()})
    except MailError:
        return [(f"the draft file {path} could not be deleted ({why}): do not send draft "
                 f"{draft_id} again, it was sent")]
    return [f"the draft file {path} could not be deleted ({why}); it is marked as sent"]


def _save_copy(srv: MailServer, mb: Mailbox, raw: bytes, password: str):
    """(where the copy of a sent mail is, warnings): "server" when the provider saves sent
    mail itself, else the Sent folder it was appended to, or None when it could not be."""
    provider = PROVIDERS.get(mb.provider)
    if provider is not None and provider.saves_sent is True:
        return "server", []
    try:
        folders = srv.folders()
        folder = _special(folders, SENT) or _named(folders, provider.sent if provider else ())
        if folder is None:
            return None, [(
                f"the mail was sent, but {mb.name} has no Sent folder (the server marks none as "
                "Sent), so no copy was saved there")]
        srv.append(folder, raw)
    except Exception as err:  # noqa: BLE001 - the mail is sent; only the copy is missing
        detail = str(err) if isinstance(err, MailError) else f"{type(err).__name__}: {err}"
        return None, [(
            "the mail was sent, but its copy could not be saved in Sent: "
            f"{_scrub(detail, password)}")]
    return folder, []


def send(hub: Path, mail_dir: Path, draft_id: str, *, connect=None) -> dict:
    """Send draft `draft_id`, which must have been shown (`show`) and not changed since, and
    delete it. `{"ok": True, "draft_id", "mailbox", "from", "to", "cc", "subject",
    "message_id", "refused", "sent_copy", "warnings"}`: `refused` are the recipients the server
    did not take, with its reply; `sent_copy` is "server" (the provider saves sent mail
    itself), the folder the copy went to, or None (see `warnings`).

    MailError, and nothing sent, for a draft never shown, changed since, or too large. A send
    that fails keeps the draft and says whether the mail may have gone out."""
    mail_dir = Path(mail_dir)
    draft = read(mail_dir, draft_id)
    again = (f"run mk draft show {draft_id}, show the user the whole text, and send only after "
             "they say so")
    if not draft["shown_hash"]:
        raise MailError(f"draft {draft_id} was never shown to the user; nothing was sent: {again}")
    contents = []
    for number, entry in enumerate(draft["attachments"], 1):
        try:
            data = _read_file(Path(entry["path"]))
        except MailError as err:
            raise MailError(
                f"draft {draft_id} changed since it was shown: attachment {number} "
                f"({entry['name']}) cannot be read any more ({err}); nothing was sent") from None
        if len(data) != entry["size"] or _sha256(data) != entry["sha256"]:
            raise MailError(
                f"draft {draft_id} changed since it was shown: the file of attachment {number} "
                f"({entry['name']}) is different now; nothing was sent: {again}")
        contents.append(data)
    if _digest(draft) != draft["shown_hash"]:
        raise MailError(f"draft {draft_id} changed since it was shown; nothing was sent: {again}")
    warning = _too_large(draft_id, draft)
    if warning:
        raise MailError(f"draft {draft_id} is too large to send: {warning}")
    mb = _mailbox_of(config.load(mail_dir), draft["address"])
    if mb is None:
        raise MailError(
            f"draft {draft_id} goes out from {draft['address']}, which is not in config.toml "
            "any more; nothing was sent")
    raw, message_id = _build(draft, contents)
    recipients = [*draft["to"], *draft["cc"]]
    password = accounts.read_password(Path(hub), mb.address)
    srv = None
    try:
        try:
            srv = (connect or server.connect)(mb, password)
            refused = dict(srv.send(raw, draft["address"], recipients) or {})
        except (LoginError, SendRefused) as err:
            raise _not_sent(draft_id, str(err), password) from None
        except ServerError as err:  # the connection broke: the server may have taken it
            raise _maybe_sent(draft_id, mb, str(err), password) from None
        except MailError as err:  # raised before anything was handed over
            raise _not_sent(draft_id, str(err), password) from None
        except Exception as err:  # noqa: BLE001 - whatever broke, the outcome is unknown
            raise _maybe_sent(draft_id, mb, f"{type(err).__name__}: {err}", password) from None
        if all(any(accounts.same_address(r, a) for a in refused) for r in recipients):
            raise _not_sent(draft_id, f"the server refused every recipient: {refused}", password)
        warnings = _forget(mail_dir, draft_id, draft)
        sent_copy, more = _save_copy(srv, mb, raw, password)
        warnings += more
    finally:
        if srv is not None:
            with contextlib.suppress(Exception):
                srv.close()
    if refused:
        warnings.insert(0, f"the server refused {len(refused)} recipient(s), who did not get "
                        "the mail: " + "; ".join(f"{a} ({why})" for a, why in refused.items()))
    return {"ok": True, "draft_id": draft_id, "mailbox": mb.name, "from": draft["address"],
            "to": draft["to"], "cc": draft["cc"], "subject": draft["subject"],
            "message_id": message_id, "refused": refused, "sent_copy": sent_copy,
            "warnings": warnings}

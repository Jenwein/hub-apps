"""Writing mail: the commands that make, change and send drafts, and the mail they build.

The agent writes a draft (`draft`, `forward`, `draft-update --body`), or the reader writes a reply
(`reply`, `revise`). Either way the draft is saved with a new code, and the whole draft goes with
the code to the user's private chat (`_publish`). The agent's output names the draft and its
addresses, never the code, and never a body the reader wrote: that is text made from a mail, and
no text from a mail may reach the agent. When the chat cannot be reached, the code is voided
again: a code nobody saw must not stay valid.

`send` checks the code (drafts.check_code) and spends it before anything goes out, then hands the
mail to himalaya with `--no-save` and appends the sent copy separately: a copy that himalaya
appended itself and failed to append would look like a failed send. A forward goes only with the
very mail the user approved: its bytes are bound to the code. `build` makes the mail 7-bit, since
himalaya stops reading the mail at its first line that is not UTF-8; himalaya takes the
recipients from To, Cc and Bcc and strips Bcc.
"""

from __future__ import annotations

import hashlib
import io
import mimetypes
import re
import unicodedata
from contextlib import suppress
from datetime import datetime
from email import message_from_bytes, policy
from email.generator import BytesGenerator
from email.headerregistry import Address
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, make_msgid
from pathlib import Path

from . import chat, clock, drafts, mailtext, query, strings
from .context import Ctx
from .drafts import MAX_ATTACH_BYTES, CodeError, Draft
from .himalaya import Himalaya, TimedOut
from .message import ATTACHMENTS_DIR
from .query import INVISIBLE, SUBJECT_LIMIT, clean
from .reader import CONTROLS, Reader
from .settings import FOLDER_ROLES, Mailbox, MailError

SEVEN_BIT = policy.SMTP.clone(cte_type="7bit")
# The mail itself: headers set with set_raw (the message ids) are written as they are. Folded by
# the 78-column policy, Python before 3.13 writes an id longer than about 76 characters (Outlook's
# are 80 to 90) as an RFC 2047 encoded word, which RFC 2047 forbids in a msg-id.
MAIL_POLICY = SEVEN_BIT.clone(refold_source="none")
# A forwarded original with 8-bit parts, written again with those parts in base64 (a text part
# with a charset) and its ASCII headers as they came.
REWRITE_POLICY = policy.compat32.clone(cte_type="7bit", linesep="\r\n")
SUBJECT_MAX = mailtext.HEADER_LIMIT
QUOTE_LIMIT = 3000  # characters of the original that a reply quotes
REFERENCES_KEPT = 20  # message ids a reply's References keeps, the last ones
LINE_MAX = 998  # the longest line of a mail (RFC 5322)
FORWARDED_NAME = "forwarded.eml"
ATTACH_NAME_LIMIT = 200  # characters of a file name, in the chat and in the mail
MESSAGE_ID = re.compile(r"<[!-;=?-~]+>")  # printable ASCII but <, > and space, in brackets
REPLY_PREFIXES = ("re:", "re：", "回复:", "回复：")
FORWARD_PREFIXES = ("fwd:", "fw:", "转发:", "转发：")
MB = 1024 * 1024
# The time himalaya has to send a mail: SEND_TIMEOUT seconds, and SEND_TIMEOUT_PER_MB more for each
# million bytes of the mail as it is built (attachments in base64 included).
SEND_TIMEOUT = 90.0
SEND_TIMEOUT_PER_MB = 30.0


# --- what the agent gives ----------------------------------------------------------------------


def _addresses(given: list[str]) -> list[str]:
    """The addresses the agent gave, each checked, in order, without repeats. An empty entry is
    left out, so that `--cc ""` empties a list."""
    found: list[str] = []
    for text in given:
        text = text.strip()
        if not text:
            continue
        if not drafts.valid_address(text):
            raise MailError(f"not an email address: {text}")
        if text.lower() not in (a.lower() for a in found):
            found.append(text)
    return found


def _mail_addresses(given: list[str]) -> list[str]:
    """The addresses of a mail's header that a reply can go to: the valid ones, without repeats.
    They come from a stranger, so a bad one is left out, never quoted in an error."""
    found: list[str] = []
    for text in given:
        if drafts.valid_address(text) and text.lower() not in (a.lower() for a in found):
            found.append(text)
    return found


def _need_to(to: list[str]) -> None:
    if not to:
        raise MailError("no recipient: give at least one --to address")


def _subject(text: str) -> str:
    return clean(text, SUBJECT_MAX)


def _text(body: str) -> str:
    """A body as the mail and the chat show it alike: `\\n` line ends, no control characters but
    tabs, none of the characters that reorder or hide text, no trailing white space."""
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    body = "".join(char for char in CONTROLS.sub("", body) if char not in INVISIBLE)
    return "\n".join(line.rstrip() for line in body.split("\n")).strip("\n")


def _attachments(ctx: Ctx, given: list[str]) -> list[str]:
    """The absolute paths of the files to attach, checked: files, not the state's own (passwords,
    the chat, the drafts) except attachments saved from mail, at most MAX_ATTACH_BYTES in all."""
    state = ctx.state.resolve()
    saved = (ctx.state / ATTACHMENTS_DIR).resolve()
    found: list[str] = []
    total = 0
    for text in given:
        if not text.strip():
            continue
        try:
            path = Path(text).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            raise MailError(f"no such file: {text}") from None
        if not path.is_file():
            raise MailError(f"not a file: {text}")
        if path.is_relative_to(state) and not path.is_relative_to(saved):
            raise MailError(f"{text} is a file of the mail state: it cannot be attached")
        check_name(path.name)
        if str(path) in found:
            continue
        found.append(str(path))
        total += path.stat().st_size
    if total > MAX_ATTACH_BYTES:
        raise MailError(
            f"the attachments are {total / MB:.1f} MB together: at most "
            f"{MAX_ATTACH_BYTES // MB} MB")
    return found


def check_name(name: str) -> None:
    """A file name the mail can carry: no control characters (a line break would break the mail
    after the code is spent), nothing that is no text, and something left to show."""
    if any(unicodedata.category(char) in ("Cc", "Cs") for char in name):
        raise MailError(f"the file name {name!r} has control characters: rename the file first")
    if not attachment_name(name):
        raise MailError(f"the file name {name!r} shows nothing: rename the file first")


def attachment_name(name: str) -> str:
    """A file name as the chat shows it and the mail carries it: without the characters that
    reorder or hide text, cut to ATTACH_NAME_LIMIT."""
    return clean(name, ATTACH_NAME_LIMIT)


# --- replies and forwards ----------------------------------------------------------------------


def _prefixed(prefix: str, subject: str, known: tuple[str, ...]) -> str:
    subject = subject.strip()
    if subject.casefold().startswith(known):
        return subject
    return f"{prefix} {subject}".strip()


def reply_subject(subject: str) -> str:
    return _prefixed("Re:", subject, REPLY_PREFIXES)


def forward_subject(subject: str) -> str:
    return _prefixed("Fwd:", subject, FORWARD_PREFIXES)


def _ids(text: str) -> list[str]:
    return MESSAGE_ID.findall(text)


def _threading(parsed: mailtext.Parsed) -> tuple[str, str]:
    """In-Reply-To and References of a reply to `parsed`: its Message-ID, and its References with
    that id after them (the last REFERENCES_KEPT ids)."""
    own = _ids(parsed.message_id)[:1]
    chain = [i for i in _ids(parsed.references) if i not in own] + own
    return (own[0] if own else ""), " ".join(chain[-REFERENCES_KEPT:])


def _when(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ""


def quote(parsed: mailtext.Parsed, language: str) -> str:
    """The original under a reply: a line that says who wrote it when, then its body (at most
    QUOTE_LIMIT characters) as `> ` lines. Empty when the original has no text."""
    text = _text(parsed.body).strip()
    if not text:
        return ""
    if len(text) > QUOTE_LIMIT:
        text = text[:QUOTE_LIMIT].rstrip() + "…"
    sender = parsed.sender_addr
    if parsed.sender_name:
        sender = f"{parsed.sender_name} <{sender}>"
    head = strings.t(
        "quote_header", language, date=_when(parsed.date) or strings.t("unknown", language),
        sender=sender or strings.t("unknown", language))
    lines = [f"> {line}" if line.strip() else ">" for line in text.split("\n")]
    return head + "\n" + "\n".join(lines)


def _with_quote(text: str, quoted: str) -> str:
    return f"{text}\n\n{quoted}" if quoted else text


def _without_quote(body: str, quoted: str) -> str:
    tail = f"\n\n{quoted}" if quoted else ""
    return body[:-len(tail)] if tail and body.endswith(tail) else body


def _source(ctx: Ctx, key: str) -> tuple[str, str, str]:
    """`(mailbox, server folder, id)` of the mail `<mailbox>/<role>/<id>`."""
    mailbox, _, rest = key.partition("/")
    role, _, id = rest.partition("/")
    if role not in FOLDER_ROLES or not id:
        raise MailError(f"{key} names no mail")
    folder = ctx.mailbox(mailbox).folders.get(role, "")
    if not folder:
        raise MailError(f"the {role} folder of {mailbox} is not known: run mx folders {mailbox}")
    return mailbox, folder, id


def _original(ctx: Ctx, hima: Himalaya, key: str) -> bytes:
    return hima.raw(*_source(ctx, key))


# --- the chat and the agent's output -----------------------------------------------------------


def _sender(mb: Mailbox) -> str:
    return f"{mb.display_name} <{mb.address}>" if mb.display_name else mb.address


def chat_text(d: Draft, mb: Mailbox, code: str, language: str) -> str:
    """The draft as the user checks it in the chat: from, to, cc, bcc, subject, the attachments,
    a blank line and the body; the code is on the last line, after everything to check."""

    def say(key: str, value: str) -> str:
        return strings.t(key, language, value=value)

    lines = [say("show_from", _sender(mb)), say("show_to", ", ".join(d.to))]
    if d.cc:
        lines.append(say("show_cc", ", ".join(d.cc)))
    if d.bcc:
        lines.append(say("show_bcc", ", ".join(d.bcc)))
    lines.append(say("show_subject", d.subject))
    names = [attachment_name(Path(path).name) for path in d.attachments]
    if d.forward_key:
        names.append(strings.t("draft_forwarded", language))
    if names:
        lines.append(strings.t("show_attachments", language, names=", ".join(names)))
    body = d.body or strings.t("show_no_text", language)
    code_line = strings.t("code_line", language, code=code, id=d.id)
    return "\n".join(lines) + "\n\n" + body + "\n" + code_line


def summary(d: Draft, mb: Mailbox) -> dict:
    """What the agent gets back: the draft without its code, and its body only when the agent
    wrote it. A subject from a mail is cut like one in a list."""
    out = {
        "draft": d.id, "mailbox": d.mailbox, "from": mb.address, "to": d.to, "cc": d.cc,
        "bcc": d.bcc, "subject": clean(d.subject, SUBJECT_LIMIT), "attachments": d.attachments,
        "written_by": d.written_by, "code_sent_to_chat": True}
    if d.written_by == "agent":
        out["body"] = d.body
    return out


def _withdraw(ctx: Ctx, id: str, issued: str) -> None:
    """Void the code `issued` of draft `id`, unless another one replaced it meanwhile."""
    with drafts.lock(ctx):
        current = drafts.load(ctx, id)
        if current.code_hash == issued:
            drafts.void(current)
            drafts.save(ctx, current)


def _publish(ctx: Ctx, target: chat.Chat, d: Draft, mb: Mailbox, code: str) -> dict:
    """Send the saved draft `d` and its new `code` to the chat. If they do not all arrive, the
    code is voided: the draft stays, and `mx confirm` gives it a new code."""
    issued = d.code_hash
    try:
        chat.send_text(target, chat_text(d, mb, code, ctx.settings.language))
    except BaseException as err:  # Ctrl+C too: whether the code arrived is not known
        with suppress(MailError):  # the state is busy or gone: the code stays, unseen
            _withdraw(ctx, d.id, issued)
        if isinstance(err, chat.ChatError):
            raise chat.ChatError(
                f"draft {d.id} is saved but the chat could not be reached: {err}") from None
        raise
    return summary(d, mb)


def _create(ctx: Ctx, target: chat.Chat, d: Draft, mb: Mailbox) -> dict:
    with drafts.lock(ctx):
        d.id = drafts.unused_id(ctx)
        code = drafts.issue_code(d, clock.now(), mb)
        drafts.save(ctx, d)
    return _publish(ctx, target, d, mb, code)


def _draft(mailbox: str, to: list[str], subject: str, body: str, **more) -> Draft:
    fields = {
        "cc": [], "bcc": [], "attachments": [], "in_reply_to": "", "references": "",
        "forward_key": "", "written_by": "agent", **more}
    return Draft(id="", mailbox=mailbox, to=to, subject=subject, body=body,
                 created_at=clock.now().isoformat(), **fields)


def _unsent(d: Draft) -> None:
    if d.sent_at:
        raise MailError(f"draft {d.id} is already sent: write a new one")


# --- the commands ------------------------------------------------------------------------------


def new_draft(ctx: Ctx, mailbox: str, to: list[str], cc: list[str], bcc: list[str],
              subject: str, body: str, attach: list[str]) -> dict:
    """`mx draft`: a mail the agent wrote."""
    mb = ctx.mailbox(mailbox)
    d = _draft(mb.name, _addresses(to), _subject(subject), _text(body), cc=_addresses(cc),
               bcc=_addresses(bcc), attachments=_attachments(ctx, attach))
    _need_to(d.to)
    if not d.body.strip():
        raise MailError("the body is empty: write it on stdin")
    return _create(ctx, chat.load(ctx), d, mb)


def reply(ctx: Ctx, hima: Himalaya, n: int, intent: str, everyone: bool,
          mailbox: str | None = None) -> dict:
    """`mx reply`: the reader writes the reply to mail `n` of the last list as `intent` says."""
    if not intent.strip():
        raise MailError("the intent is empty: give --intent TEXT")
    found = query.item(ctx, n)
    mb = ctx.mailbox(mailbox or found.mailbox)
    target = chat.load(ctx)  # before the server is asked and the reader paid for
    parsed = mailtext.parse(hima.raw(found.mailbox, found.folder, found.id))
    to = _mail_addresses(parsed.reply_to) or _mail_addresses([parsed.sender_addr])
    if not to:
        raise MailError(f"#{n} has no address a reply can go to")
    cc: list[str] = []
    if everyone:
        skip = {a.lower() for a in to} | {mb.address.lower()}
        cc = [a for a in _mail_addresses(parsed.to + parsed.cc) if a.lower() not in skip]
    language = ctx.settings.language
    text = _text(Reader(ctx.settings.reader_model).draft_reply(parsed, intent, language))
    in_reply_to, references = _threading(parsed)
    d = _draft(
        mb.name, to, _subject(reply_subject(parsed.subject)),
        _with_quote(text, quote(parsed, language)), cc=cc, in_reply_to=in_reply_to,
        references=references, written_by="reader", reply_key=found.key, intent=intent.strip())
    return _create(ctx, target, d, mb)


def forward(ctx: Ctx, hima: Himalaya, n: int, to: list[str], note: str,
            mailbox: str | None = None) -> dict:
    """`mx forward`: mail `n` of the last list goes as an attachment, under the agent's note."""
    addresses = _addresses(to)
    _need_to(addresses)
    found = query.item(ctx, n)
    mb = ctx.mailbox(mailbox or found.mailbox)
    target = chat.load(ctx)
    raw = hima.raw(found.mailbox, found.folder, found.id)
    parsed = mailtext.parse(raw)  # for its whole subject
    d = _draft(mb.name, addresses, _subject(forward_subject(parsed.subject)), _text(note),
               forward_key=found.key, forward_sha256=hashlib.sha256(raw).hexdigest())
    return _create(ctx, target, d, mb)


def revise(ctx: Ctx, hima: Himalaya, id: str, instruction: str) -> dict:
    """`mx revise`: the reader rewrites its reply as the user's `instruction` says."""
    if not instruction.strip():
        raise MailError("the instruction is empty: give --instruction TEXT")
    d = drafts.load(ctx, id)
    _unsent(d)
    if d.written_by != "reader":
        raise MailError("you wrote this draft: change it with mx draft-update")
    if not d.reply_key:
        raise MailError(f"draft {id} does not say which mail it answers: write a new reply")
    mb = ctx.mailbox(d.mailbox)
    target = chat.load(ctx)
    parsed = mailtext.parse(_original(ctx, hima, d.reply_key))
    language = ctx.settings.language
    quoted = quote(parsed, language)
    text = _text(Reader(ctx.settings.reader_model).draft_reply(
        parsed, d.intent, language, previous=_without_quote(d.body, quoted),
        instruction=instruction))
    with drafts.lock(ctx):
        current = drafts.load(ctx, id)
        if current.body != d.body or current.written_by != "reader" or current.sent_at:
            raise MailError(f"draft {id} changed meanwhile: run mx revise again")
        current.body = _with_quote(text, quoted)
        code = drafts.issue_code(current, clock.now(), mb)
        drafts.save(ctx, current)
    return _publish(ctx, target, current, mb, code)


def update(ctx: Ctx, id: str, *, to: list[str] | None = None, cc: list[str] | None = None,
           bcc: list[str] | None = None, subject: str | None = None,
           attach: list[str] | None = None, body: str | None = None) -> dict:
    """`mx draft-update`: a given list replaces the old one; a new body makes it the agent's."""
    if to is None and cc is None and bcc is None and subject is None and attach is None and (
            body is None):
        raise MailError("nothing to change: give --to, --cc, --bcc, --subject, --attach or --body")
    changes: dict[str, object] = {}
    for name, given in (("to", to), ("cc", cc), ("bcc", bcc)):
        if given is not None:
            changes[name] = _addresses(given)
    if subject is not None:
        changes["subject"] = _subject(subject)
    if attach is not None:
        changes["attachments"] = _attachments(ctx, attach)
    if body is not None:
        changes["body"] = _text(body)
        if not changes["body"].strip():
            raise MailError("the body is empty: write it on stdin")
        changes["written_by"] = "agent"
    if "to" in changes:
        _need_to(changes["to"])
    target = chat.load(ctx)
    with drafts.lock(ctx):
        d = drafts.load(ctx, id)
        _unsent(d)
        mb = ctx.mailbox(d.mailbox)
        for name, value in changes.items():
            setattr(d, name, value)
        code = drafts.issue_code(d, clock.now(), mb)
        drafts.save(ctx, d)
    return _publish(ctx, target, d, mb, code)


def confirm(ctx: Ctx, id: str) -> dict:
    """`mx confirm`: a new code for the draft as it is (the old one expired, was void, or the
    send failed)."""
    target = chat.load(ctx)
    with drafts.lock(ctx):
        d = drafts.load(ctx, id)
        _unsent(d)
        mb = ctx.mailbox(d.mailbox)
        code = drafts.issue_code(d, clock.now(), mb)
        drafts.save(ctx, d)
    return _publish(ctx, target, d, mb, code)


def send(ctx: Ctx, hima: Himalaya, id: str, code: str) -> dict:
    """`mx send`: the draft goes out if `code` is its code. The code is spent before anything is
    sent, so that no failure or crash leaves a used code valid. A forward goes only with the
    original the draft was made from: its bytes must be the same on the server still."""
    first = drafts.load(ctx, id)
    ctx.mailbox(first.mailbox)  # a mailbox that is gone fails before a try is counted
    if first.sent_at:
        raise CodeError("already sent")
    original = None
    if first.forward_key and first.code_hash:  # fetched before the lock: the server may be slow
        original = _original(ctx, hima, first.forward_key)
    with drafts.lock(ctx):
        d = drafts.load(ctx, id)
        mb = ctx.mailbox(d.mailbox)
        files = drafts.read_files(d)  # the bytes that are checked are the bytes that are sent
        try:
            drafts.check_code(d, code, clock.now(), mb, files)
        except CodeError:
            drafts.save(ctx, d)  # a wrong try counts
            raise
        if d.forward_key and (d.forward_key != first.forward_key or original is None):
            # It got its code, or another original, after `first` was read: the code holds.
            raise MailError(f"draft {id} changed meanwhile: run mx send again")
        if d.forward_key and hashlib.sha256(original).hexdigest() != d.forward_sha256:
            raise MailError(
                f"the mail that draft {id} forwards changed on the server since the draft was "
                "made: make a new forward")
        drafts.void(d)
        drafts.save(ctx, d)
        try:
            raw = build(d, mb, original, files)
            hima.send(d.mailbox, raw, timeout=send_timeout(len(raw)))
        except TimedOut as err:  # the server may have taken the mail before the time ran out
            raise MailError(_maybe_sent(id, mb, err)) from err
        except (MailError, ValueError) as err:
            raise MailError(f"not sent: {err}; run mx confirm {id} for a new code") from err
        d.sent_at = clock.now().isoformat()
        d.message_id = _message_id(raw)
        drafts.save(ctx, d)
    result = {"sent": id, "message_id": d.message_id}
    if mb.save_copy:
        failed = _copy(hima, mb, raw)
        if failed:
            result["copy_failed"] = failed  # the mail went out all the same
    return result


def send_timeout(size: int) -> float:
    """Seconds himalaya has to send a mail of `size` bytes: a large one takes a while to upload."""
    return SEND_TIMEOUT + SEND_TIMEOUT_PER_MB * size / 1_000_000


def _maybe_sent(id: str, mb: Mailbox, err: TimedOut) -> str:
    """What a send that ran out of time says: the mail may have gone out, so it must be looked
    for before a new code could send it a second time. Where mx appends the sent copy itself, the
    sent folder has none of a send that did not answer, and only the recipient can tell."""
    if mb.save_copy:
        look = (
            f"{mb.name} gets its sent copy from mx after a send that answered, so its sent folder "
            "cannot tell: ask the recipient whether it arrived")
    else:
        look = f"look for it with mx list --mailbox {mb.name} --folder sent, or ask the recipient"
    return (
        f"draft {id} may have been sent: {err}. The code is spent, and the mail may have gone "
        f"out: {look}, before you run mx confirm {id} for a new code")


def listing(ctx: Ctx) -> dict:
    """`mx drafts`: the drafts not sent yet."""
    now = clock.now()
    return {"drafts": [
        {"id": d.id, "mailbox": d.mailbox, "to": d.to, "subject": clean(d.subject, SUBJECT_LIMIT),
         "written_by": d.written_by, "waiting_for_code": drafts.waiting(d, now),
         "created_at": d.created_at}
        for d in drafts.pending(ctx)]}


def discard(ctx: Ctx, id: str) -> dict:
    with drafts.lock(ctx):
        drafts.discard(ctx, id)
    return {"discarded": id}


def _copy(hima: Himalaya, mb: Mailbox, raw: bytes) -> str:
    """Append the sent mail to the sent folder, marked as read; why it failed, else ""."""
    folder = mb.folders.get("sent", "")
    if not folder:
        return f"the sent folder of {mb.name} is not known: run mx folders {mb.name}"
    try:
        hima.add(mb.name, folder, raw)
    except MailError as err:
        return str(err)
    return ""


# --- the mail ----------------------------------------------------------------------------------


def _domain(address: str) -> str:
    """The domain of `address` for a Message-ID, in ASCII."""
    domain = address.rpartition("@")[2].strip() or "localhost"
    if domain.isascii():
        return domain
    try:
        return domain.encode("idna").decode("ascii")
    except UnicodeError:
        return "localhost"


def _message_id(raw: bytes) -> str:
    head = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    return str(head.get("Message-ID", "")).strip()


def _seven_bit(text: bytes) -> bool:
    return text.isascii() and all(len(line) <= LINE_MAX for line in text.split(b"\r\n"))


def _rewritten(text: bytes) -> bytes | None:
    """`text`, a mail with 8-bit parts, written again in 7 bits: each text part with a charset
    in base64, the headers that are ASCII as they came. None when that fails."""
    try:
        out = io.BytesIO()
        BytesGenerator(out, mangle_from_=False, policy=REWRITE_POLICY).flatten(
            message_from_bytes(text, policy=policy.compat32))
    except mailtext.PARSE_ERRORS:
        return None
    return out.getvalue()


def _forwarded(original: bytes) -> EmailMessage:
    """The original of a forward as an attachment. A message/rfc822 part in 7 bits, the only
    encoding RFC 2046 allows there: as it is when it is 7-bit already, else written again with its
    8-bit parts encoded. When that cannot be done, the exact bytes as a file `forwarded.eml`."""
    part = EmailMessage(policy=SEVEN_BIT)
    text = original.replace(b"\r\n", b"\n").replace(b"\r", b"\n").replace(b"\n", b"\r\n")
    if not _seven_bit(text):
        text = _rewritten(text) or b""
    if text and _seven_bit(text):
        part["Content-Type"] = "message/rfc822"
        part["Content-Transfer-Encoding"] = "7bit"
        part["Content-Disposition"] = f'attachment; filename="{FORWARDED_NAME}"'
        part.set_payload(text.decode("ascii"))
    else:
        part.set_content(
            original, maintype="application", subtype="octet-stream",
            disposition="attachment", filename=FORWARDED_NAME)
    return part


def build(d: Draft, mb: Mailbox, original: bytes | None,
          files: list[tuple[str, bytes]] | None = None) -> bytes:
    """The mail of draft `d` from mailbox `mb`, as 7-bit bytes with CRLF line ends: the files of
    `files` (read now when None) attached, and `original` attached as a message when it is a
    forward."""
    if files is None:
        files = drafts.read_files(d)
    msg = EmailMessage(policy=MAIL_POLICY)
    msg["From"] = Address(display_name=mb.display_name, addr_spec=mb.address)
    msg["To"] = ", ".join(d.to)
    if d.cc:
        msg["Cc"] = ", ".join(d.cc)
    if d.bcc:
        msg["Bcc"] = ", ".join(d.bcc)  # himalaya sends to it and takes the line out
    msg["Subject"] = d.subject
    msg["Date"] = format_datetime(clock.now())
    # The message ids as they are (MAIL_POLICY), one per line; only well-formed ids get in.
    msg.set_raw("Message-ID", make_msgid(domain=_domain(mb.address)))
    replied = _ids(d.in_reply_to)[:1]
    if replied:
        msg.set_raw("In-Reply-To", replied[0])
    chain = _ids(d.references)
    if chain:
        msg.set_raw("References", "\n ".join(chain))
    msg.set_content(d.body)
    for path, data in files:
        name = attachment_name(Path(path).name)
        kind, encoding = mimetypes.guess_type(name)
        if kind is None or encoding is not None or kind.startswith("multipart/"):
            kind = "application/octet-stream"
        maintype, subtype = kind.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    if original is not None:
        if msg.get_content_type() != "multipart/mixed":
            msg.make_mixed()
        msg.attach(_forwarded(original))
    return msg.as_bytes()

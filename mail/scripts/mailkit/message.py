"""What the commands do with a mail of the last list: show it, look at it, ask about it, read the
codes of the newest mails, list and send its attachments, mark it as read and move it.

The agent must not read a mail. The original (`show`), the answer to a question (`ask`) and a file
(`attachment-send`) therefore go to the user's private chat and not to stdout, and `look` and
`code` give the agent only the fixed fields of the reader's verdict (see reader.py). Reading never
marks a mail as read: `Himalaya.raw` and `fetch_raw` peek. Only `mark_read` sets the flag, and
the only thing a mail is ever moved to is the archive or trash folder: nothing is deleted.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import UTC, timedelta
from pathlib import Path

from . import chat, clock, mailtext, query, strings
from .context import Ctx
from .himalaya import AuthError, ConnectError, Himalaya, HimalayaError
from .paths import write_private
from .query import EPOCH, INVISIBLE, Item, clean, when
from .reader import ATTACH_LIMIT, CONTROLS, Reader
from .settings import MailError

SHOW_LIMIT = 40_000  # characters of the body that `show` sends: ten chat messages, not a flood
CODE_LIMIT = 20  # mails of one mailbox that `code` looks at, the newest
SEARCH_BACK_DAYS = 2  # `code` asks for mail dated after this many days before its window
MOVE_TARGETS = ("archive", "trash")
ATTACHMENTS_DIR = "attachments"  # in the state directory
FILE_NAME_LIMIT = 100
VERDICT_FIELDS = (
    "sender", "subject", "date", "summary", "code", "amount", "deadline", "tier", "category",
    "reason", "phishing")

# What a file name must not hold on any system, nor in a command line that cmd.exe reads (on
# Windows cc-connect may be a `.cmd` launcher): the characters Windows refuses in a name and
# `& ^ % !`. Path separators are handled before, not here.
UNSAFE_NAME_CHARS = re.compile(r'[<>:"|?*&^%!]')
SEPARATORS = re.compile(r"[\\/]")
# Names that Windows opens as devices, with any extension (CON.txt too); the superscript digits
# count as digits there.
DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"{kind}{digit}" for kind in ("COM", "LPT") for digit in "0123456789¹²³"})


def _read(hima: Himalaya, found: Item) -> bytes:
    return hima.raw(found.mailbox, found.folder, found.id)


def _failure(err: HimalayaError) -> str:
    """Why a mailbox is missing from a result, as `mx list` says it."""
    if isinstance(err, AuthError):
        return f"login failed: {err}"
    if isinstance(err, ConnectError):
        return f"cannot connect: {err}"
    return str(err)


# --- show --------------------------------------------------------------------------------------


def _plain(text: str) -> str:
    """`text` for the chat: without control characters (but tabs and line breaks) and without the
    characters that reorder or hide text."""
    return "".join(char for char in CONTROLS.sub("", text) if char not in INVISIBLE)


def original(parsed: mailtext.Parsed, language: str) -> str:
    """The mail as the chat shows it: From, To, Cc, Date and Subject, the attachments, a blank
    line and the body (cut to SHOW_LIMIT characters, and a line says so)."""
    sender = parsed.sender_addr
    if parsed.sender_name:
        sender = f"{parsed.sender_name} <{sender}>"
    lines = [f"From: {sender}"]
    if parsed.to:
        lines.append("To: " + ", ".join(parsed.to))
    if parsed.cc:
        lines.append("Cc: " + ", ".join(parsed.cc))
    lines.append(f"Date: {parsed.date or strings.t('unknown', language)}")
    lines.append(f"Subject: {parsed.subject}")
    if parsed.attachments:
        names = "; ".join(
            f"{a.index}. {a.filename} ({a.content_type}, {a.size} bytes)"
            for a in parsed.attachments)
        lines.append(strings.t("show_attachments", language, names=names))
    body = _plain(parsed.body).strip()
    if not body:
        body = strings.t("show_no_text", language)
    elif len(body) > SHOW_LIMIT:
        body = body[:SHOW_LIMIT] + "\n" + strings.t("show_cut", language, limit=SHOW_LIMIT)
    return "\n".join(lines) + "\n\n" + body


def show(ctx: Ctx, hima: Himalaya, n: int) -> dict:
    """Send the original of mail `n` to the private chat."""
    target = chat.load(ctx)  # before the server is asked: no chat, no mail fetched
    found = query.item(ctx, n)
    parsed = mailtext.parse(_read(hima, found))
    chat.send_text(target, original(parsed, ctx.settings.language))
    return {"n": n, "shown_in_chat": True}


# --- look and ask ------------------------------------------------------------------------------


def look(ctx: Ctx, hima: Himalaya, n: int) -> dict:
    """The reader's verdict on mail `n`, its attachments read too: fixed fields only. The user's
    rules arrive with `rules.combined`; until then the reader gets none."""
    found = query.item(ctx, n)
    raw = _read(hima, found)
    verdict = Reader(ctx.settings.reader_model).look(
        found.key, found.mailbox, mailtext.parse(raw), mailtext.attachment_texts(raw, ATTACH_LIMIT),
        "", ctx.settings.language)
    return {"n": n, "verdict": {name: getattr(verdict, name) for name in VERDICT_FIELDS}}


def ask(ctx: Ctx, hima: Himalaya, n: int, question: str) -> dict:
    """The reader's answer to `question` about mail `n`, sent to the private chat."""
    if not question.strip():
        raise MailError("the question is empty: give --question TEXT")
    target = chat.load(ctx)  # before the reader is paid for
    found = query.item(ctx, n)
    raw = _read(hima, found)
    answer = Reader(ctx.settings.reader_model).answer(
        mailtext.parse(raw), mailtext.attachment_texts(raw, ATTACH_LIMIT), question,
        ctx.settings.language)
    heading = strings.t("answer_about", ctx.settings.language, subject=found.subject)
    chat.send_text(target, f"{heading}\n{answer}")
    return {"n": n, "answered_in_chat": True}


# --- code --------------------------------------------------------------------------------------


def codes(ctx: Ctx, hima: Himalaya, names: list[str], minutes: int) -> dict:
    """The verification codes in the inbox mails of the last `minutes` minutes of the named
    mailboxes (all when none), newest first. The reader sorts all of them in one call; a mailbox
    that cannot be asked is in `errors`."""
    boxes = [ctx.mailbox(name) for name in dict.fromkeys(names) or ctx.mailboxes]
    start = clock.now() - timedelta(minutes=minutes)
    start_at = (start - EPOCH).total_seconds()
    # `after` compares the day of the Date header in the sender's own time zone, which is a day
    # off at most: ask for mail from two days before the window, and filter on the exact time.
    day = start.astimezone(UTC).date() - timedelta(days=SEARCH_BACK_DAYS)
    errors: dict[str, str] = {}
    mails: list[tuple[float, str, str, mailtext.Parsed]] = []  # when, key, mailbox, parsed
    for box in boxes:
        folder = box.folders.get("inbox", "")
        if not folder:
            errors[box.name] = (
                f"the inbox folder of {box.name} is not known: run mx folders {box.name}")
            continue
        try:
            recent = {
                int(e.id): when(e)
                for e in hima.search(box.name, folder, ["after", day.isoformat()], CODE_LIMIT)
                if e.id.isascii() and e.id.isdigit() and e.date is not None
                and when(e) >= start_at}
            raws = hima.fetch_raw(box.name, folder, sorted(recent))
        except HimalayaError as err:
            errors[box.name] = _failure(err)
            continue
        mails.extend(
            (recent[uid], f"{box.name}/inbox/{uid}", box.name, mailtext.parse(raw))
            for uid, raw in raws.items() if uid in recent)
    mails.sort(key=lambda mail: mail[0], reverse=True)  # stable: ties keep mailbox order
    verdicts = Reader(ctx.settings.reader_model).triage(
        [(key, mailbox, parsed) for _, key, mailbox, parsed in mails], "", ctx.settings.language)
    return {
        "codes": [
            {"mailbox": v.mailbox, "sender": v.sender, "subject": v.subject, "date": v.date,
             "code": v.code}
            for v in verdicts if v.code],
        "errors": errors}


# --- attachments -------------------------------------------------------------------------------


def safe_filename(name: str, fallback: str) -> str:
    """A name that is safe to save a file under, made from the (attacker-chosen) `name` of an
    attachment: the last part of it as a path with `/` or `\\`, no control or invisible
    characters, none of `<>:"|?*&^%!`, no trailing dot or space, not a Windows device name, at
    most FILE_NAME_LIMIT characters. `fallback` when nothing is left."""
    text = clean(name, 10 * len(name) + 1)  # long enough to cut nothing
    pieces = [piece.strip().rstrip(". ") for piece in SEPARATORS.split(text)]
    base = UNSAFE_NAME_CHARS.sub("_", next((piece for piece in reversed(pieces) if piece), ""))
    if not base:
        return fallback
    if len(base) > FILE_NAME_LIMIT:
        dot = base.rfind(".")
        ext = base[dot:] if 0 < dot and len(base) - dot <= 16 else ""
        base = base[:FILE_NAME_LIMIT - len(ext)].rstrip(". ") + ext
    if base.split(".", 1)[0].rstrip(" ").upper() in DEVICE_NAMES:
        base = "_" + base
    return base


def attachments(ctx: Ctx, hima: Himalaya, n: int) -> dict:
    """The attachments of mail `n`: number, file name, type and size."""
    parsed = mailtext.parse(_read(hima, query.item(ctx, n)))
    return {"n": n, "attachments": [asdict(a) for a in parsed.attachments]}


def save_attachment(ctx: Ctx, key: str, name: str, data: bytes) -> Path:
    """Save `data` as `state/attachments/<key with / as _>/<name>`; `name` is made safe first and
    the path is checked to be inside its folder."""
    root = ctx.state / ATTACHMENTS_DIR
    folder = root / safe_filename(key.replace("/", "_"), "mail")
    path = folder / name
    if (path.parent.resolve().parent != root.resolve()
            or path.resolve().parent != folder.resolve()):
        raise MailError("the attachment's name would leave its folder: it is not saved")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        write_private(path, data)
    except OSError as err:
        raise MailError(f"cannot save the attachment: {err}") from err
    return path


def attachment_send(ctx: Ctx, hima: Himalaya, n: int, index: int) -> dict:
    """Save attachment `index` of mail `n` in the state directory and send the file to the
    private chat."""
    target = chat.load(ctx)
    found = query.item(ctx, n)
    attachment, data = mailtext.attachment_bytes(_read(hima, found), index)
    if not data:
        raise MailError(f"attachment {index} of #{n} is empty or cannot be read")
    name = safe_filename(attachment.filename, f"attachment-{index}")
    chat.send_file(target, save_attachment(ctx, found.key, name, data))
    return {"sent_file": name}


# --- mark and move -----------------------------------------------------------------------------


def mark_read(ctx: Ctx, hima: Himalaya, n: int) -> dict:
    """Mark mail `n`, and no other, as read."""
    found = query.item(ctx, n)
    hima.mark_seen(found.mailbox, found.folder, found.id)
    return {"marked_read": n}


def move(ctx: Ctx, hima: Himalaya, n: int, to: str) -> dict:
    """Move mail `n` to the archive or the trash folder of its mailbox. Nothing is deleted."""
    if to not in MOVE_TARGETS:
        raise MailError(f"a mail moves to {' or '.join(MOVE_TARGETS)}, not to {to}")
    found = query.item(ctx, n)
    folder = ctx.mailbox(found.mailbox).folders.get(to, "")
    if not folder:
        raise MailError(f"{found.mailbox} has no {to} folder set")
    hima.move(found.mailbox, found.folder, found.id, folder)
    return {"moved": n, "to": folder}

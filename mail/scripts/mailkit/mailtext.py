"""Mail as text: the headers, the body and the attachments of a raw message.

`parse` reads what the reader and the chat need from the exact bytes of a mail: the sender and
recipients, the date, the subject, the ids a reply refers to, the body as plain text, and the list
of attached files. Odd mail is the rule, not the exception: a mail may have only an HTML part,
headers and bodies in GB18030 or GBK (common from QQ and Chinese senders), raw 8-bit headers, a
charset that is wrong or unknown, or no text at all. Text that does not decode in its declared
charset is read as UTF-8, then as GB18030, and only then with replacement characters.

Nothing here runs or opens a file: an attachment's text comes only from `text/*` parts and from
PDFs, which pdfplumber reads from memory; every other type has no text. No link is ever fetched:
HTML keeps the text of a link and drops its address.
"""

from __future__ import annotations

import io
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.errors import MessageError
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesHeaderParser
from email.utils import getaddresses, parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser

from .query import clean
from .settings import MailError

FILENAME_LIMIT = 100
HEADER_LIMIT = 998  # a header value, as one line (the line length of RFC 5322)
ADDRESS_LIMIT = 200  # addresses read per header name (To, Cc, ...): a mail may list thousands
ADDRESS_CHARS = 20_000  # of each raw address header, before the stdlib parser sees it
PDF_PAGES = 10  # the pages of a PDF that are read
PDF_MAX_BYTES = 5 * 1024 * 1024  # a bigger PDF is not read at all: slow, and a likely trap

# Lone surrogates: what the parser makes of bytes it could not decode (U+DC80 to U+DCFF stand
# for the bytes 0x80 to 0xFF), and what no text may carry out of here.
SURROGATES = re.compile("[\ud800-\udfff]")
TEXT_TYPES = ("text/plain", "text/html")
PDF_TYPES = ("application/pdf", "application/x-pdf")

# What the stdlib parsers raise on hostile input where they usually record a defect. No mail may
# crash a command: on any of these a header is kept as it came, and a part has no text. The parser
# and the writer recurse once per level of nesting, so a crafted mail of thousands of nested parts
# raises RecursionError.
PARSE_ERRORS = (
    AssertionError, AttributeError, IndexError, KeyError, LookupError, TypeError, ValueError,
    MessageError, RecursionError)


@dataclass(frozen=True)
class Attachment:
    index: int  # from 1, in MIME order: what `attachment_bytes` takes
    filename: str  # cleaned and cut to FILENAME_LIMIT; `attachment-<index>` when it has none
    content_type: str
    size: int  # bytes, decoded


@dataclass(frozen=True)
class Parsed:
    sender_name: str
    sender_addr: str
    to: list[str]  # addresses only
    cc: list[str]
    reply_to: list[str]
    date: str  # ISO 8601, empty when the mail has no usable Date
    subject: str
    message_id: str
    references: str
    body: str
    attachments: list[Attachment]


# --- decoding ----------------------------------------------------------------------------------


def _decode(data: bytes, charset: str | None) -> str:
    """`data` in its declared charset; when that fails (or none is declared), UTF-8, then
    GB18030, then UTF-8 with replacement characters. Newlines become `\\n`."""
    text = None
    for name in dict.fromkeys(n for n in (charset, "utf-8", "gb18030") if n):
        try:
            text = data.decode(name)
            break
        except (LookupError, ValueError):  # an unknown charset, or bytes not in it
            continue
    if text is None:
        text = data.decode("utf-8", "replace")
    # A charset such as unicode-escape can make lone surrogates, which no output can encode.
    text = SURROGATES.sub("\ufffd", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _unescape(text: str) -> str:
    """`text` with the raw bytes the parser kept as lone surrogates decoded like a body."""
    if not SURROGATES.search(text):
        return text
    try:
        data = text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:  # a surrogate that stands for no byte
        return SURROGATES.sub("\ufffd", text)
    return _decode(data, None)


def _words(text: str) -> str:
    """RFC 2047 encoded words decoded, where the parser left them (in a file name, for one)."""
    if "=?" not in text:
        return text
    try:
        return str(make_header(decode_header(text)))
    except PARSE_ERRORS:  # a malformed word: keep the text as it came
        return text


def _line(text: str) -> str:
    return clean(_unescape(text), HEADER_LIMIT)


# --- headers -----------------------------------------------------------------------------------


def _raw_values(msg: Message, name: str) -> list[str]:
    """The values of header `name` as they are in the mail, before any parsing."""
    return [value for key, value in msg.raw_items() if key.lower() == name.lower()]


def _header(msg: Message, name: str) -> str:
    """The first `name` header as one line, "" when missing: encoded words decoded, and raw 8-bit
    bytes read as UTF-8 or GB18030."""
    raw = _raw_values(msg, name)
    if not raw:
        return ""
    if SURROGATES.search(raw[0]):  # raw 8-bit bytes: the parser would make them U+FFFD
        return _line(raw[0])
    try:
        return _line(str(msg[name]))
    except PARSE_ERRORS:  # a header the parser cannot make sense of: the text as it is
        return _line(raw[0])


def _cut_list(value: str) -> str:
    """A raw address list cut to ADDRESS_CHARS characters, at the last comma before the cut so no
    address is split. The stdlib address parser gets slower than linear with the length."""
    if len(value) <= ADDRESS_CHARS:
        return value
    head = value[:ADDRESS_CHARS]
    comma = head.rfind(",")
    return head[:comma] if comma > 0 else head


def _addresses(msg: Message, name: str) -> list[tuple[str, str]]:
    """(display name, address) of the first ADDRESS_LIMIT addresses in the `name` headers. A mail
    can list hundreds of thousands: each header is cut (`_cut_list`) before it is parsed, and no
    further header is parsed once the limit is reached."""
    found: list[tuple[str, str]] = []
    for value in _raw_values(msg, name):
        if len(found) >= ADDRESS_LIMIT:
            break
        value = _cut_list(value)
        try:
            header = msg.policy.header_fetch_parse(name, value)
            pairs = [(a.display_name, a.addr_spec) for a in header.addresses]
        except PARSE_ERRORS:  # a header the parser cannot make sense of
            pairs = getaddresses([_unescape(value)])
        # A name the parser left encoded (it gives up on a word that holds a line break).
        found += [(_line(_words(n)), _line(a)) for n, a in pairs if _line(a)]
    return found[:ADDRESS_LIMIT]


def _date(msg: Message) -> str:
    text = _header(msg, "Date")
    try:
        return parsedate_to_datetime(text).isoformat() if text else ""
    except (TypeError, ValueError, IndexError, OverflowError):
        return ""


# --- parts -------------------------------------------------------------------------------------


def _leaves(msg: Message) -> Iterator[Message]:
    """The parts that hold content, in MIME order. An attached message (message/rfc822) is one
    part: what it holds is not the mail's. A loop, not recursion: nesting may be deep."""
    stack = [msg]
    while stack:
        part = stack.pop()
        payload = part.get_payload() if part.get_content_maintype() == "multipart" else None
        if isinstance(payload, list):
            stack.extend(reversed(payload))
        else:
            yield part


def _filename(part: Message) -> str | None:
    try:
        name = part.get_filename()
    except PARSE_ERRORS:  # a parameter the parser cannot read
        return None
    return None if name is None else _unescape(_words(str(name)))


def _is_text_of_mail(part: Message) -> bool:
    """A plain or HTML part that is the mail's own text (one of its versions), not a file."""
    return (
        part.get_content_type() in TEXT_TYPES
        and part.get_content_disposition() != "attachment"
        and _filename(part) is None)


def _payload(part: Message) -> bytes:
    """The bytes a part holds, transfer encoding undone; an attached message as a whole."""
    if part.is_multipart():  # message/rfc822: its parts were parsed, give the message back
        try:
            inner = part.get_payload(0)
            return inner.as_bytes() if isinstance(inner, Message) else b""
        except PARSE_ERRORS:  # a message that cannot be written back, or nests too deep for it
            return b""
    try:
        data = part.get_payload(decode=True)
    except PARSE_ERRORS:  # a transfer encoding that cannot be undone
        data = None
    return data if isinstance(data, bytes) else b""


def _text(part: Message) -> str:
    try:
        charset = part.get_content_charset()
    except PARSE_ERRORS:
        charset = None
    return _decode(_payload(part), charset)


def _structure(msg: Message) -> tuple[Message | None, list[Message]]:
    """The body part and the attachments. The body is the first plain part of the mail's text,
    else the first HTML one; a plain part with nothing in it gives way to the HTML. Every part
    that is not the mail's text is an attachment."""
    leaves = list(_leaves(msg))
    texts = [part for part in leaves if _is_text_of_mail(part)]
    plain = [p for p in texts if p.get_content_type() == "text/plain"]
    html = [p for p in texts if p.get_content_type() == "text/html"]
    body = plain[0] if plain else None
    if html and (body is None or not _text(body).strip()):
        body = html[0]
    return body, [part for part in leaves if not _is_text_of_mail(part)]


def _read(raw: bytes) -> tuple[Message, Message | None, list[Message]]:
    """The mail, its body part and its attachments. The stdlib parser recurses once per level of
    nesting: a mail it cannot parse (thousands of nested parts) is read for its headers only, with
    no body and no attachments."""
    try:
        msg = message_from_bytes(raw, policy=policy.default)
    except PARSE_ERRORS:
        return BytesHeaderParser(policy=policy.default).parsebytes(raw), None, []
    return msg, *_structure(msg)


def _attachment(index: int, part: Message) -> Attachment:
    name = clean(_filename(part) or "", FILENAME_LIMIT)
    return Attachment(
        index=index,
        filename=name or f"attachment-{index}",
        content_type=clean(part.get_content_type(), FILENAME_LIMIT),
        size=len(_payload(part)))


def _body(part: Message | None) -> str:
    if part is None:
        return ""
    text = _text(part)
    return html_to_text(text) if part.get_content_type() == "text/html" else text


def parse(raw: bytes) -> Parsed:
    """The mail of the exact bytes `raw`. Header values are one line each, without control or
    invisible characters; the body is plain text and is not cut (the reader cuts it)."""
    msg, body, files = _read(raw)
    senders = _addresses(msg, "From")
    name, addr = senders[0] if senders else ("", "")
    return Parsed(
        sender_name=name,
        sender_addr=addr,
        to=[a for _, a in _addresses(msg, "To")],
        cc=[a for _, a in _addresses(msg, "Cc")],
        reply_to=[a for _, a in _addresses(msg, "Reply-To")],
        date=_date(msg),
        subject=_header(msg, "Subject"),
        message_id=_header(msg, "Message-ID"),
        references=_header(msg, "References"),
        body=_body(body),
        attachments=[_attachment(i, part) for i, part in enumerate(files, 1)],
    )


# --- HTML --------------------------------------------------------------------------------------

# Tags that end a line where they start and where they end.
BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "br", "caption", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table", "title", "tr", "ul",
})
CELL_TAGS = frozenset({"td", "th"})
SKIPPED_TAGS = frozenset({"script", "style"})  # their content is no text
TAG = re.compile(r"<[^>]*>")
SPACES = re.compile(r"\s+")


class _TextParser(HTMLParser):
    """Collects the text of an HTML document. Attributes are never looked at, so no address of a
    link or an image gets into the text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipped = 0  # inside script or style
        self.pre = 0  # inside pre: white space is kept

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in SKIPPED_TAGS:
            self.skipped += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in CELL_TAGS:
            self.parts.append(" ")
        if tag == "pre":
            self.pre += 1

    def handle_startendtag(self, tag: str, attrs: list) -> None:  # <br/>: one break, not two
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIPPED_TAGS:
            self.skipped = max(0, self.skipped - 1)
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "pre":
            self.pre = max(0, self.pre - 1)

    def handle_data(self, data: str) -> None:
        if not self.skipped:
            self.parts.append(data if self.pre else SPACES.sub(" ", data))


def html_to_text(html: str) -> str:
    """The text of an HTML document: script and style dropped, block tags as line breaks, links
    as their text only, entities unescaped, white space collapsed, no empty lines."""
    parser = _TextParser()
    try:
        parser.feed(html)
        parser.close()
        text = "".join(parser.parts)
    except PARSE_ERRORS:  # markup the parser gives up on: strip the tags by hand
        text = unescape(TAG.sub(" ", html))
    lines = (SPACES.sub(" ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


# --- attachments -------------------------------------------------------------------------------


def _pdf_text(data: bytes, limit: int) -> str:
    """The text of the first PDF_PAGES pages, read from memory; "" for a PDF over PDF_MAX_BYTES
    and for one pdfplumber cannot read (broken, encrypted). pdfplumber is asked for those pages
    only, so it makes no page object for the others."""
    if len(data) > PDF_MAX_BYTES:
        return ""
    import pdfplumber  # here: a slow import that only PDFs need

    logging.getLogger("pdfminer").setLevel(logging.ERROR)  # no warnings about odd PDFs on stderr
    pages: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(data), pages=list(range(1, PDF_PAGES + 1))) as pdf:
            for page in pdf.pages:
                pages.append(page.extract_text() or "")
                if sum(len(p) for p in pages) >= limit:
                    break
    except Exception:  # pdfminer raises many kinds of error on a broken or hostile file
        logging.getLogger(__name__).debug("PDF not read", exc_info=True)
    return "\n".join(pages)


def _file_text(part: Message, attachment: Attachment, limit: int) -> str:
    kind = attachment.content_type
    if kind.startswith("text/"):
        text = _text(part)
        text = html_to_text(text) if kind == "text/html" else text
    elif kind in PDF_TYPES or (
            kind == "application/octet-stream" and attachment.filename.lower().endswith(".pdf")):
        text = _pdf_text(_payload(part), limit)
    else:
        return ""
    return text.strip()[:limit]


def attachment_texts(raw: bytes, limit: int) -> list[tuple[Attachment, str]]:
    """Every attachment with its text cut to `limit` characters: the text of `text/*` parts and
    of PDFs; "" for every other type, which is never opened."""
    _, _, files = _read(raw)
    found = []
    for index, part in enumerate(files, 1):
        attachment = _attachment(index, part)
        found.append((attachment, _file_text(part, attachment, limit)))
    return found


def attachment_bytes(raw: bytes, index: int) -> tuple[Attachment, bytes]:
    """Attachment number `index` (from 1, as `Parsed.attachments` numbers them) and its bytes."""
    _, _, files = _read(raw)
    if not 1 <= index <= len(files):
        raise MailError(f"the mail has no attachment {index}: it has {len(files)}")
    part = files[index - 1]
    return _attachment(index, part), _payload(part)

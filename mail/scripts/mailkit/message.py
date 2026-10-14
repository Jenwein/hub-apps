"""One raw RFC 822 message as the mail app uses it: headers, readable text and attachments.

`parse` turns the bytes a server returns into a `Parsed`. Mail from Chinese providers is the hard
case: GBK and GB18030 bodies with a wrong or missing charset, raw 8-bit bytes in headers, HTML-only
bodies, attachment names in RFC 2231 or in encoded words. `parse` does not raise on bad bytes; a
text that cannot be decoded cleanly still comes out, with replacement characters at worst, because
a mail that cannot be read must not stop the poll of its mailbox.

Everything in a message is data from a stranger. `parse` only reads it.
"""

from __future__ import annotations

import email
import email.errors
import email.parser
import email.policy
import email.utils
import re
from dataclasses import dataclass, field
from datetime import UTC
from html.parser import HTMLParser

from .config import MailError

TEXT_LIMIT = 100_000  # characters of `Parsed.text`
HTML_LIMIT = 2_000_000  # characters of one HTML body handed to the HTML parser

# tried in this order after the charset the part declares; UTF-8 first because bytes that are not
# GBK-shaped but are valid UTF-8 (text that declares nothing) almost never are GBK, while GBK text
# is almost never valid UTF-8
FALLBACK_CHARSETS = ("utf-8", "gb18030")

# What the stdlib's mail code has raised on hostile input: IndexError in the parameter parser,
# TypeError in the RFC 2231 continuation sorter, ValueError for a NUL in a charset name,
# RecursionError for multiparts nested thousands deep. Bad mail must not stop a poll.
STDLIB_ERRORS = (
    ArithmeticError, AttributeError, LookupError, RecursionError, TypeError, ValueError,
    email.errors.MessageError,
)


@dataclass
class Attachment:
    index: int  # position among the message's attachments; what `attachment_bytes` takes
    filename: str  # a plain name: no directory part
    content_type: str
    size: int  # bytes after transfer decoding


@dataclass
class Parsed:
    message_id: str | None = None  # "<id@host>"
    date: str | None = None  # ISO-8601 with the sender's offset; None when missing or unreadable
    from_addr: str = ""
    from_name: str = ""
    to: list[str] = field(default_factory=list)  # addresses only
    cc: list[str] = field(default_factory=list)
    subject: str = ""
    text: str = ""  # the readable body, at most TEXT_LIMIT characters
    attachments: list[Attachment] = field(default_factory=list)
    bulk: bool = False  # list or bulk mail: a hint for triage, not a rule
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)


# -- decoding --------------------------------------------------------------------------------


def decode(data: bytes, charset: str | None = None) -> str:
    """The text of `data`: in the declared charset if the bytes fit it, else UTF-8, else GB18030,
    else UTF-8 with replacement characters. Never raises."""
    for name in (charset, *FALLBACK_CHARSETS):
        if not name:
            continue
        try:
            return data.decode(name)
        except (LookupError, ValueError):  # unknown charset; bytes that do not fit it
            continue
    return data.decode("utf-8", errors="replace")


_SURROGATES = re.compile("[\udc80-\udcff]")  # what raw 8-bit bytes of a header become
_LINE_BREAKS = re.compile(r"\r\n|\r|\n")


class _Policy(email.policy.EmailPolicy):
    """`email.policy.default`, except that a header whose parser gives up (the stdlib's have
    raised IndexError on `Content-Type: text/html; charset`) is read as the plain text it is, the
    way the old compat32 policy read every header, instead of stopping the whole parse."""

    def header_fetch_parse(self, name, value):
        try:
            return super().header_fetch_parse(name, value)
        except STDLIB_ERRORS:
            text = _LINE_BREAKS.sub("", value)
            return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")

    def header_store_parse(self, name, value):
        try:
            return super().header_store_parse(name, value)
        except STDLIB_ERRORS:
            return name, value


POLICY = _Policy()
_WRITE_POLICY = email.policy.compat32.clone(linesep="\r\n")


def _repair_headers(part) -> None:
    """Replace raw 8-bit header values (a GBK subject, a GBK attachment name) by their text.

    The parser keeps such bytes as surrogate escapes, which the header classes turn into U+FFFD,
    so the text has to be recovered before anything reads the header."""
    names = {name.lower() for name, value in part.raw_items() if _SURROGATES.search(value)}
    for lowered in names:
        headers = [(key, value) for key, value in part.raw_items() if key.lower() == lowered]
        del part[lowered]
        for key, value in headers:
            text = decode(value.encode("utf-8", errors="surrogateescape"))
            try:
                part[key] = _LINE_BREAKS.sub("", text)
            except ValueError:  # a second copy of a header that may occur once
                continue


def _load(raw: bytes):
    """The message tree of `raw`, with readable headers."""
    try:
        msg = email.message_from_bytes(raw, policy=POLICY)
    except STDLIB_ERRORS:  # the headers are kept, the body is not
        msg = email.parser.BytesHeaderParser(policy=POLICY).parsebytes(raw)
        msg.set_payload("")
    stack = [msg]
    while stack:
        part = stack.pop()
        _repair_headers(part)
        if part.is_multipart():
            stack.extend(part.get_payload())
    return msg


# -- text ------------------------------------------------------------------------------------

# soft hyphen, combining grapheme joiner, zero-width and bidi marks, BOM: marketing mail pads
# its preview text with them, and a bidi override can disguise a file name
_INVISIBLE = re.compile("[\u00ad\u034f\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", _INVISIBLE.sub("", text))
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:TEXT_LIMIT]


def _one_line(text: str) -> str:
    return " ".join(_INVISIBLE.sub("", _CONTROL.sub("", text)).split())


class _HtmlText(HTMLParser):
    """HTML to the text a reader would see: no script, style or title; a line break where a
    block starts or ends (a blank line around paragraphs, headings and tables) and a space
    between table cells."""

    HIDDEN = frozenset({"script", "style", "title"})
    LINES = frozenset({
        "address", "article", "aside", "dd", "div", "dl", "dt", "fieldset", "figcaption",
        "figure", "footer", "form", "header", "hr", "li", "main", "nav", "section", "tr",
    })
    PARAGRAPHS = frozenset({
        "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "ol", "p", "pre", "table", "ul",
    })
    CELLS = frozenset({"td", "th"})

    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []
        self.hidden = 0  # open script/style/title elements
        self.pre = 0  # open pre elements
        self.pending = 0  # line breaks owed before the next text, 0 to 2

    def _break(self, tag: str) -> None:
        if tag == "br":
            self.pending = min(self.pending + 1, 2)
        elif tag in self.PARAGRAPHS:
            self.pending = 2
        elif tag in self.LINES:
            self.pending = max(self.pending, 1)

    def handle_starttag(self, tag, attrs):
        if tag in self.HIDDEN:
            self.hidden += 1
        else:
            if tag == "pre":
                self.pre += 1
            self._break(tag)

    def handle_startendtag(self, tag, attrs):
        # "<br/>" is one break, and "<script/>" opens nothing that an end tag would close
        if tag not in self.HIDDEN:
            self._break(tag)

    def handle_endtag(self, tag):
        if tag in self.HIDDEN:
            self.hidden = max(0, self.hidden - 1)
            return
        if tag == "pre":
            self.pre = max(0, self.pre - 1)
        if tag in self.CELLS:
            if not self.pending:
                self.out.append(" ")
        elif tag != "br":
            self._break(tag)

    def handle_data(self, data):
        if self.hidden:
            return
        if not self.pre:
            data = re.sub(r"\s+", " ", data)
        if not data.strip():  # the white space between two tags
            if not self.pending:
                self.out.append(data)
            return
        if self.pending:
            self.out.append("\n" * self.pending)
            self.pending = 0
            data = data.lstrip()
        self.out.append(data)

    def text(self) -> str:
        lines = "".join(self.out).split("\n")
        return "\n".join(" ".join(line.split()) for line in lines)


def _html_to_text(html: str) -> str:
    parser = _HtmlText()
    parser.feed(html[:HTML_LIMIT])
    parser.close()
    return parser.text()


def _part_bytes(part) -> bytes:
    """The bytes of a part after transfer decoding; of a message/rfc822 part, that message
    written out again (the old compat32 policy writes headers as they are, without parsing)."""
    if part.is_multipart():
        inner = part.get_payload()
        try:
            return inner[0].as_bytes(policy=_WRITE_POLICY) if inner else b""
        except STDLIB_ERRORS:
            return b""
    return part.get_payload(decode=True) or b""


def _part_text(part) -> str:
    try:
        charset = part.get_content_charset()
    except STDLIB_ERRORS:
        charset = None
    return decode(_part_bytes(part), charset)


# -- parts -----------------------------------------------------------------------------------


def _leaves(root):
    """The parts that hold content, in message order. A message/rfc822 part is one leaf."""
    stack = [root]
    while stack:
        part = stack.pop()
        if part.get_content_maintype() == "multipart" and part.is_multipart():
            stack.extend(reversed(part.get_payload()))
        else:
            yield part


def _filename(part) -> str | None:
    try:
        name = part.get_filename()
    except STDLIB_ERRORS:  # an unreadable name is still a name that exists
        name = "attachment"
    if not name:
        return None
    # the name is shown and used as a file name later: the last path component, no control or
    # direction-changing characters
    plain = re.sub(r"[\x00-\x1f\x7f]", "", _INVISIBLE.sub("", re.split(r"[\\/]", name)[-1]))
    return plain.strip() or "attachment"


def _sort_parts(msg) -> tuple[list, list, list[tuple[str, object]]]:
    """(plain text parts, HTML parts, [(file name, part)]) of a message."""
    plain, html, attached = [], [], []
    for part in _leaves(msg):
        name = _filename(part)
        if name:
            attached.append((name, part))
        elif part.get_content_disposition() == "attachment":
            continue
        elif part.get_content_type() == "text/html":
            html.append(part)
        elif part.get_content_type() == "text/plain" or part.get_content_maintype() == "multipart":
            # (a multipart that could not be split, for lack of a boundary, is read as text)
            plain.append(part)
    return plain, html, attached


def _body_text(plain: list, html: list) -> str:
    text = _clean("\n\n".join(t for t in map(_part_text, plain) if t.strip()))
    if text or not html:
        return text
    return _clean("\n\n".join(_html_to_text(_part_text(part)) for part in html))


def attachment_bytes(raw: bytes, index: int) -> tuple[str, bytes]:
    """(file name, content) of attachment `index` of the message `raw`."""
    attached = _sort_parts(_load(raw))[2]
    if not 0 <= index < len(attached):
        raise MailError(f"the message has no attachment {index} (it has {len(attached)})")
    name, part = attached[index]
    return name, _part_bytes(part)


# -- headers ---------------------------------------------------------------------------------


def _header(msg, name: str) -> str:
    value = msg[name]
    return "" if value is None else str(value)


def _addresses(msg, name: str) -> list[tuple[str, str]]:
    """[(display name, address)] of every mailbox in the header(s) `name`; groups are flattened."""
    found = []
    for header in msg.get_all(name, []):
        addresses = getattr(header, "addresses", None)
        if addresses is None:  # the header class gave up (see _Policy): the old parser reads it
            found.extend(email.utils.getaddresses([str(header)]))
        else:
            found.extend((a.display_name, a.addr_spec) for a in addresses)
    return [(display, addr) for display, addr in found if addr]


_ID = re.compile(r"<[^<>\s]+>")


def _iso(value: str) -> str | None:
    try:
        moment = email.utils.parsedate_to_datetime(value)
    except STDLIB_ERRORS:
        return None
    if moment.tzinfo is None:  # "-0000": the zone is unknown, which is as good as UTC
        moment = moment.replace(tzinfo=UTC)
    return moment.isoformat()


def _first_id(value: str) -> str | None:
    found = _ID.search(value)
    return found.group() if found else None


def _is_bulk(msg) -> bool:
    return (
        "list-unsubscribe" in msg
        or "list-id" in msg
        or _header(msg, "precedence").strip().lower() in ("bulk", "list")
    )


def parse(raw: bytes) -> Parsed:
    """The record of one message. Never raises on bad bytes."""
    msg = _load(raw)
    plain, html, attached = _sort_parts(msg)
    from_name, from_addr = next(iter(_addresses(msg, "from")), ("", ""))
    return Parsed(
        message_id=_first_id(_header(msg, "message-id")),
        date=_iso(_header(msg, "date")),
        from_addr=from_addr,
        from_name=_one_line(from_name),
        to=[addr for _, addr in _addresses(msg, "to")],
        cc=[addr for _, addr in _addresses(msg, "cc")],
        subject=_one_line(_header(msg, "subject")),
        text=_body_text(plain, html),
        attachments=[
            Attachment(i, name, part.get_content_type(), len(_part_bytes(part)))
            for i, (name, part) in enumerate(attached)
        ],
        bulk=_is_bulk(msg),
        in_reply_to=_first_id(_header(msg, "in-reply-to")),
        references=_ID.findall(_header(msg, "references")),
    )


def _bare(address: str) -> str:
    """The address in lower case without a `+tag` in the local part."""
    local, at, domain = address.strip().lower().rpartition("@")
    if not at:
        return address.strip().lower()
    head = local.partition("+")[0]
    return f"{head or local}@{domain}"


def addressed(parsed: Parsed, address: str) -> str:
    """How the mail reached `address`: "to", "cc" or "none". Case and `+tag` do not matter."""
    wanted = _bare(address)
    if any(_bare(a) == wanted for a in parsed.to):
        return "to"
    if any(_bare(a) == wanted for a in parsed.cc):
        return "cc"
    return "none"

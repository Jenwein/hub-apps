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
import email.header
import email.parser
import email.policy
import email.utils
import re
from dataclasses import dataclass, field
from datetime import UTC
from html import unescape

from .config import MailError

TEXT_LIMIT = 100_000  # characters of `Parsed.text`
HTML_LIMIT = 300_000  # characters of one HTML body that are converted to text
# The stdlib's header parsers are slow on hostile values: quadratic on quotes and semicolons
# (50 000 quotes in a To: 3 s, 14 s on Python 3.12.10) and about 7 microseconds per character on
# anything else, for each header that is read. An address list can really be long (hundreds of
# recipients), so an address header is cut at HEADER_LIMIT characters; at most a handful are read.
# The parameters of Content-Type, Content-Disposition and Content-Transfer-Encoding are short: up
# to PARSE_LIMIT characters (a file name of forty Chinese characters, percent-encoded) they go to
# the header classes, like every other header, as long as the message has PARSE_BUDGET characters
# of them left to spend. A longer value, or any value once the budget is spent, is cut at
# PARAM_LIMIT and read as the plain text it is by the parameter parser of `email.message`, which
# is fast on any input and understands RFC 2231; RFC 2047 words in a file name are decoded by
# `_filename`. The budget is what bounds a message of thousands of parts: the FeedParser reads
# the Content-Type of every part it builds, in message order, so the first parts get the exact
# parse. Spent per distinct header and per message, not per read.
HEADER_LIMIT = 10_000
PARSE_LIMIT = 500
PARSE_BUDGET = 60_000  # characters; about half a second of worst-case input
PARAM_LIMIT = 2_000
MAX_PARAMS = 64  # a file name in RFC 2231 form needs about 40 parameters; 2 000 semicolons: 1 s
MAX_MARKS = 2_000  # in an address header of 10 000 characters; 10 000 hostile quotes: 0.6 s
_LIMITS = {
    **dict.fromkeys((
        "from", "to", "cc", "bcc", "sender", "reply-to",
        "resent-from", "resent-to", "resent-cc", "resent-bcc", "resent-sender",
    ), HEADER_LIMIT),
    **dict.fromkeys(
        ("content-type", "content-disposition", "content-transfer-encoding"), PARAM_LIMIT),
}
# The tree of a message has at most this many parts (the message, multiparts and attached
# messages included). The rest are ignored: they are neither body nor listed attachments. The
# tree is pruned after the FeedParser has built it, so what bounds the time of a message of
# thousands of parts is PARSE_BUDGET (the FeedParser reads the Content-Type of every part), and
# what this bounds is the work after that. Real mail has a few parts; 200 is a mail with well
# over a hundred attachments.
MAX_PARTS = 200
# A message has one From, To and Cc header; a copy or two is a mailer's mistake, a thousand is an
# attack on the parser (each copy of a hostile address header is a parse of up to HEADER_LIMIT).
MAX_ADDRESS_HEADERS = 2

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


def _plain(value: str) -> str:
    """The text of a header that is read as plain text, the way the old compat32 policy read
    every header: its parser gave up, or it is too long to be parsed."""
    text = _LINE_BREAKS.sub("", value)
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


# the characters an address parser is quadratic in: quotes, backslashes, and commas
_MARKS = re.compile(r'["\\,]')


def _cut(value: str, limit: int) -> str:
    """`value` cut to `limit` characters, and for parameters also before the MAX_PARAMS-th
    semicolon, for addresses before the MAX_MARKS-th quote, backslash or comma (the parsers slow
    down with their number as well). An address list is cut between two addresses."""
    if limit == PARAM_LIMIT:
        at = -1
        for _ in range(MAX_PARAMS):
            at = value.find(";", at + 1, limit)
            if at < 0:
                return value[:limit]
        return value[:at]
    cut = value[:limit]
    for number, mark in enumerate(_MARKS.finditer(cut), 1):
        if number == MAX_MARKS:
            cut = cut[:mark.start()]
            break
    if len(cut) == len(value):
        return value
    return cut.rpartition(",")[0] or cut


class _Work:
    """What the message being read has cost the header classes. A part is asked its type,
    charset, disposition and file name many times and the stdlib parses the header anew each
    time, so the header objects of the parameter headers are kept here, once per distinct value
    (the message needs three per part at most); the budget is charged when one is made. Address
    headers are read once and are not kept: a hostile one is megabytes of parse tree. Reset when
    a message is loaded and when `parse` or `attachment_bytes` is done with it."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.left = PARSE_BUDGET
        self.headers: dict[tuple[str, str], object] = {}


_WORK = _Work()


def _parse_header(name: str, value: str):
    """The header object for `value`, or its text when the stdlib's parser gives up."""
    try:
        return email.policy.EmailPolicy.header_fetch_parse(POLICY, name, value)
    except STDLIB_ERRORS:
        return _plain(value)


def _parameter_header(name: str, value: str):
    """A Content-Type, Content-Disposition or Content-Transfer-Encoding: parsed by the header
    classes while the budget lasts, else, or when it is long, read as plain text."""
    long = len(value) > PARSE_LIMIT  # (before the cut: a cut can leave a short hostile value)
    value = _cut(value, PARAM_LIMIT)
    if long:
        return _plain(value)
    key = (name, value)
    header = _WORK.headers.get(key)
    if header is None:
        if len(value) > _WORK.left:
            return _plain(value)
        _WORK.left -= len(value)
        header = _WORK.headers[key] = _parse_header(name, value)
    return header


class _Policy(email.policy.EmailPolicy):
    """`email.policy.default`, except that a header whose parser gives up (the stdlib's have
    raised IndexError on `Content-Type: text/html; charset`) is read as the plain text it is, the
    way the old compat32 policy read every header, instead of stopping the whole parse; and
    except that the headers with parsers that are slow on hostile values are cut and budgeted
    (`_LIMITS`, `_Work`)."""

    def header_fetch_parse(self, name, value):
        if hasattr(value, "name"):  # a header object that was set is already parsed
            return value
        limit = _LIMITS.get(name.lower())
        if limit == PARAM_LIMIT:
            return _parameter_header(name, value)
        if limit is not None:
            return _parse_header(name, _cut(value, limit))
        try:
            return super().header_fetch_parse(name, value)
        except STDLIB_ERRORS:
            return _plain(value)


POLICY = _Policy()
_WRITE_POLICY = email.policy.compat32.clone(linesep="\r\n")


def _repair_headers(part) -> None:
    """Replace raw 8-bit header values (a GBK subject, a GBK attachment name) by their text.

    The parser keeps such bytes as surrogate escapes, which the header classes turn into U+FFFD,
    so the text has to be recovered before anything reads the header. The text is stored as it
    is, like the value of any other header, and parsed when a header is read: the headers nobody
    reads cost nothing, however many there are or however long (the header classes are slow on
    hostile values). All copies of a header that has such a value in one copy are replaced; one
    pass makes the new list, so thousands of such headers take time linear in their number."""
    items = list(part.raw_items())
    names = {key.lower() for key, value in items if _SURROGATES.search(value)}
    if not names:
        return
    copies: dict[str, int] = {}
    fixed = []
    for key, value in items:
        lowered = key.lower()
        if lowered not in names:
            fixed.append((key, value))
            continue
        most = POLICY.header_max_count(key)  # 1 for To, Subject and the like
        if most and copies.get(lowered, 0) >= most:
            continue  # a second copy of a header that may occur once
        copies[lowered] = copies.get(lowered, 0) + 1
        text = decode(value.encode("utf-8", errors="surrogateescape"))
        fixed.append((key, _LINE_BREAKS.sub("", text)))
    part._headers = fixed  # (`Message` keeps its headers in this list; `del part[name]` is O(n))


def _load(raw: bytes):
    """The message tree of `raw`, with readable headers, of at most MAX_PARTS parts."""
    _WORK.reset()
    try:
        msg = email.message_from_bytes(raw, policy=POLICY)
    except STDLIB_ERRORS:  # the headers are kept, the body is not
        msg = email.parser.BytesHeaderParser(policy=POLICY).parsebytes(raw)
        msg.set_payload("")
    admitted = 1
    stack = [msg]
    while stack:
        part = stack.pop()
        _repair_headers(part)
        if part.is_multipart():
            children = part.get_payload()  # (the list itself)
            del children[max(MAX_PARTS - admitted, 0):]
            admitted += len(children)
            stack.extend(reversed(children))  # message order, so that the first parts are kept
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


class _TextWriter:
    """What the HTML-to-text conversion collects: a line break where a block starts or ends (a
    blank line around paragraphs, headings and tables) and a space between table cells."""

    LINES = frozenset({
        "address", "article", "aside", "dd", "div", "dl", "dt", "fieldset", "figcaption",
        "figure", "footer", "form", "header", "hr", "li", "main", "nav", "section", "tr",
    })
    PARAGRAPHS = frozenset({
        "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "ol", "p", "pre", "table", "ul",
    })
    CELLS = frozenset({"td", "th"})

    def __init__(self) -> None:
        self.out: list[str] = []
        self.pre = 0  # open pre elements
        self.pending = 0  # line breaks owed before the next text, 0 to 2

    def lone(self, tag: str) -> None:
        """A tag that opens nothing to close ("<br>", "<br/>") or an end tag."""
        if tag == "br":
            self.pending = min(self.pending + 1, 2)
        elif tag in self.PARAGRAPHS:
            self.pending = 2
        elif tag in self.LINES:
            self.pending = max(self.pending, 1)

    def start(self, tag: str) -> None:
        if tag == "pre":
            self.pre += 1
        self.lone(tag)

    def end(self, tag: str) -> None:
        if tag == "pre":
            self.pre = max(0, self.pre - 1)
        if tag in self.CELLS:
            if not self.pending:
                self.out.append(" ")
        elif tag != "br":
            self.lone(tag)

    def data(self, data: str) -> None:
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


# One token of HTML. Every alternative consumes what it scans (a tag or comment that never
# closes runs to the end of the input), so a pass over the text is linear. The stdlib's
# HTMLParser is not on every Python this runs on: on 3.12.10 it takes 25 s for 80 000 "<!--" and
# minutes for 100 000 "<a ", and it raises AssertionError on "<![ x>".
_HTML_TOKEN = re.compile(
    r"<!--.*?(?:-->|\Z)"  # comment
    r"|<[!?][^>]*(?:>|\Z)"  # doctype, CDATA section, processing instruction
    r"|<(?P<slash>/?)(?P<name>[A-Za-z][^\s/>]*)(?P<rest>[^>]*)(?:>|\Z)"  # tag
    r"|(?P<text>[^<]+)"
    r"|(?P<lt><)",  # a "<" that starts nothing
    re.DOTALL,
)
# the text of these elements is not shown; the search for their end tag is a plain scan
_HIDDEN_ENDS = {
    name: re.compile(rf"</{name}\b[^>]*>?", re.IGNORECASE) for name in ("script", "style", "title")
}


def _html_to_text(html: str) -> str:
    """The text a reader of the HTML would see. Never raises, and takes time linear in the size."""
    writer = _TextWriter()
    html = html[:HTML_LIMIT]
    pos, size = 0, len(html)
    unclosed = set()  # hidden elements whose end tag was searched for and is not there
    while pos < size:
        token = _HTML_TOKEN.match(html, pos)
        pos = token.end()
        if token["text"] is not None:
            writer.data(unescape(token["text"]))
        elif token["lt"] is not None:
            writer.data("<")
        elif token["name"] is not None:
            name = token["name"].lower()
            if token["slash"]:
                writer.end(name)
            elif token["rest"].rstrip().endswith("/"):
                writer.lone(name)
            elif name in _HIDDEN_ENDS:
                found = None if name in unclosed else _HIDDEN_ENDS[name].search(html, pos)
                if found:
                    pos = found.end()
                else:
                    unclosed.add(name)
                    if name != "title":  # an unclosed script or style hides the rest, as in a browser
                        pos = size
            else:
                writer.start(name)
    return writer.text()


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


def _decode_words(name: str) -> str:
    """`name` with its RFC 2047 words decoded (in the charset they name, GBK and the like
    included). The header classes do that for a short header; a long one is read as plain text
    and its file name arrives with the words in it, and the base64 in them has slashes."""
    if "=?" not in name:
        return name
    try:
        pieces = email.header.decode_header(name)
    except STDLIB_ERRORS:  # bad base64 and the like: the name stays as it is
        return name
    return "".join(
        decode(data, charset) if isinstance(data, bytes) else data for data, charset in pieces)


def _filename(part) -> str | None:
    try:
        name = part.get_filename()
        name = name and _decode_words(name)
    except STDLIB_ERRORS:  # an unreadable name is still a name that exists
        name = "attachment"
    if not name:
        return None
    # the name is shown and used as a file name later: the last path component, no control or
    # direction-changing characters
    plain = re.sub(r"[\x00-\x1f\x7f]", "", _INVISIBLE.sub("", re.split(r"[\\/]", name)[-1]))
    return plain.strip() or "attachment"


def _attachments(msg) -> list[tuple[str, object]]:
    """[(file name, part)] of a message, in message order."""
    found = []
    for part in _leaves(msg):
        name = _filename(part)
        if name:
            found.append((name, part))
    return found


def _has_text(segments: list[tuple[str, str]]) -> bool:
    return any(kind == "html" or text.strip() for kind, text in segments)


def _best(alternatives: list[list[tuple[str, str]]]) -> list[tuple[str, str]]:
    """The one alternative of a multipart/alternative to read: the first that is all plain text
    and not blank, else the first with anything to read (HTML)."""
    for segments in alternatives:
        if _has_text(segments) and all(kind == "plain" for kind, _ in segments):
            return segments
    for segments in alternatives:
        if _has_text(segments):
            return segments
    return []


def _segments(part) -> list[tuple[str, str]]:
    """[(kind, source text)] of the body of a part in message order, kind "plain" or "html".

    Alternatives are chosen where they are (per multipart/alternative), so a plain footer next
    to an HTML body adds to the body instead of replacing it. Attachments are not body."""
    if part.get_content_maintype() == "multipart" and part.is_multipart():
        children = [_segments(child) for child in part.get_payload()]
        if part.get_content_subtype() == "alternative":
            return _best(children)
        return [segment for child in children for segment in child]
    if _filename(part) or part.get_content_disposition() == "attachment":
        return []
    if part.get_content_type() == "text/html":
        return [("html", _part_text(part))]
    if part.get_content_type() == "text/plain" or part.get_content_maintype() == "multipart":
        # (a multipart that could not be split, for lack of a boundary, is read as text)
        return [("plain", _part_text(part))]
    return []


def _body_text(msg) -> str:
    texts = []
    for kind, source in _segments(msg):
        text = _html_to_text(source) if kind == "html" else source
        if text.strip():
            texts.append(text)
    return _clean("\n\n".join(texts))


def _attachment(raw: bytes, index: int) -> tuple[str, bytes]:
    attached = _attachments(_load(raw))
    if not 0 <= index < len(attached):
        raise MailError(f"the message has no attachment {index} (it has {len(attached)})")
    name, part = attached[index]
    return name, _part_bytes(part)


def attachment_bytes(raw: bytes, index: int) -> tuple[str, bytes]:
    """(file name, content) of attachment `index` of the message `raw`. Raises MailError when
    there is no such attachment or the message cannot be read."""
    try:
        return _attachment(raw, index)
    except MailError:
        raise
    except Exception as error:
        # the contract is that only MailError comes out, whatever the stdlib raises on hostile mail
        raise MailError(f"attachment {index} cannot be read ({type(error).__name__})") from error
    finally:
        _WORK.reset()  # (nothing of the message is kept)


# -- headers ---------------------------------------------------------------------------------


def _header(msg, name: str) -> str:
    value = msg[name]
    return "" if value is None else str(value)


def _raw(msg, name: str) -> str:
    """The text of the first header `name`, unfolded, without the header classes. For ids, the
    date and Precedence there is nothing to decode, and the unstructured parser the stdlib would
    use is quadratic on a long References."""
    for key, value in msg.raw_items():
        if key.lower() == name:
            return _LINE_BREAKS.sub("", str(value))
    return ""


def _addresses(msg, name: str) -> list[tuple[str, str]]:
    """[(display name, address)] of every mailbox in the header(s) `name`; groups are flattened.
    Only the first MAX_ADDRESS_HEADERS copies are read: `get_all` parses every copy."""
    found = []
    copies = [item for item in msg.raw_items() if item[0].lower() == name]
    for key, value in copies[:MAX_ADDRESS_HEADERS]:
        header = msg.policy.header_fetch_parse(key, value)
        addresses = getattr(header, "addresses", None)
        if addresses is None:  # the header class gave up (see _Policy): the old parser reads it
            try:
                found.extend(email.utils.getaddresses([str(header)]))
            except STDLIB_ERRORS:  # it recurses too, on a few thousand nested comments
                continue
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
        or _raw(msg, "precedence").strip().lower() in ("bulk", "list")
    )


def _parse(raw: bytes) -> Parsed:
    msg = _load(raw)
    attached = _attachments(msg)
    try:
        text = _body_text(msg)
    except STDLIB_ERRORS:  # a tree nested too deep to walk: the headers are still worth having
        text = ""
    from_name, from_addr = next(iter(_addresses(msg, "from")), ("", ""))
    return Parsed(
        message_id=_first_id(_raw(msg, "message-id")),
        date=_iso(_raw(msg, "date")),
        from_addr=from_addr,
        from_name=_one_line(from_name),
        to=[addr for _, addr in _addresses(msg, "to")],
        cc=[addr for _, addr in _addresses(msg, "cc")],
        subject=_one_line(_header(msg, "subject")),
        text=text,
        attachments=[
            Attachment(i, name, part.get_content_type(), len(_part_bytes(part)))
            for i, (name, part) in enumerate(attached)
        ],
        bulk=_is_bulk(msg),
        in_reply_to=_first_id(_raw(msg, "in-reply-to")),
        references=_ID.findall(_raw(msg, "references")),
    )


def parse(raw: bytes) -> Parsed:
    """The record of one message. Never raises: a message nothing can be made of comes out as an
    empty record rather than stopping the poll of its mailbox."""
    try:
        return _parse(raw)
    except Exception:  # noqa: BLE001
        # the contract is that nothing comes out, whatever the stdlib raises on hostile mail
        return Parsed()
    finally:
        _WORK.reset()  # (nothing of the message is kept)


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

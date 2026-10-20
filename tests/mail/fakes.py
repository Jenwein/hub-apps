"""Bodies of the fake executables of the mail tests, and the objects that wrap them.

A body is the text of a Python script that the `make_exe` fixture runs behind a shim, so it
works on every platform. More fakes arrive with the tasks that need them.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# How the fakes of himalaya and claude add a line to their call log. mx may run several of them
# at once (triage sorts batches on threads), and on Windows the appends of two processes can tear
# each other's lines: a directory made with mkdir, which only one process can make, is the lock
# around the append.
LOG_APPEND = r'''
import os as _os
import time as _time


def append_log(path, text):
    lock = path + ".lock"
    deadline = _time.monotonic() + 30
    while True:
        try:
            _os.mkdir(lock)
            break
        except (FileExistsError, PermissionError):  # Windows denies one that is being removed
            if _time.monotonic() > deadline:
                raise
            _time.sleep(0.005)
    try:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(text)
    finally:
        _os.rmdir(lock)
'''

# How the fakes of himalaya and carillon tell that their stdin is the null device (or closed), as
# mx promises: a pipe, a file or a console is not.
STDIN_NULL = r'''
import os as _os
import stat as _stat
import sys as _sys


def stdin_is_null():
    try:
        if not _stat.S_ISCHR(_os.fstat(0).st_mode):
            return False
        if _sys.platform != "win32":
            return not _os.isatty(0)
        import ctypes
        import msvcrt
        from ctypes import wintypes

        get_mode = ctypes.WinDLL("kernel32", use_last_error=True).GetConsoleMode
        get_mode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_mode.restype = wintypes.BOOL
        return not get_mode(msvcrt.get_osfhandle(0), ctypes.byref(wintypes.DWORD()))
    except (OSError, ValueError):
        return True  # no stdin at all
'''


# The fake himalaya (v2.2.1) answers from a JSON "server" file and logs every call.
#
# The server file:
#     {"<mailbox>": {"<folder>": {"uidvalidity": "1", "uidnext": 40,
#                                 "messages": [{"uid": 1, "date": "...", "internal_date": "...",
#                                               "seen": false, "raw": "<eml text>"}]}},
#      "fail": {"<mailbox>": <failure> | {"imap": <failure>, "smtp": <failure>}}}
# - `<mailbox>` is the account name (`-a`), `<folder>` the folder name as the server gives it
#   (modified UTF-7 included). `uidnext` is optional (default: highest UID + 1).
# - `date` is the Date header and `internal_date` the received time, RFC 3339 or RFC 2822; each
#   falls back to the other, and `date` to the header of `raw`.
# - `raw` is the mail as UTF-8 text; for bytes that are not UTF-8 use `raw_b64` (base64 of the
#   exact bytes) or a `raw` with lone surrogates `\udcXX` (surrogateescape), which round-trip.
# - `flags` (optional, a list such as ["answered"]) holds the flags other than seen.
# - `fail_fetch` (optional) is a <failure> (see `fail` below) of every `imap fetch --body` that
#   names the message: a mail whose bytes cannot be fetched.
# - `fail`: a <failure> is an error text, or [error, source, ...] for an error with causes. It
#   makes every command of that account exit 1 with it on stdout: the F-CLI JSON with `--json`,
#   else the layout of the real binary (`Error: ...`, `Caused by:` with ` - ...` items,
#   `Suggestions:`). `account check` reports both backends failing instead (exit 0). A dict fails
#   one backend: "imap" for every command but `message send`, "smtp" for `message send`.
# - `message send` appends {"mailbox", "raw"} to the top-level list "_sent" (what a send would
#   deliver, cut at the first line that is not UTF-8, as himalaya does); `message add` and
#   `message move` change the folders.
#
# The call log has one JSON line per call: `argv` (the whole sys.argv: argv[0] is this script, so
# argv[1:5] is `-c himalaya.toml -a <mailbox>`), `cwd`, `stdin_closed` (stdin is the null device
# or closed), `stdin` (what `message send` and `message add` read, else null) and `exit`.
#
# Exit 99 with `F-NODEL` on stdout: `message delete`, `imap expunge` and `--seen` (except as the
# filter of `imap search`), with himalaya's aliases (`msg`, `rm`) and `imap delete`, `imap close`,
# `imap raw`. Exit 97: the fake itself crashed. A command or option it does not know exits 2.
# `himalaya --version` prints `himalaya v<$FAKE_HIMALAYA_VERSION, default 2.2.1> +imap +smtp` and
# is not logged.
FAKE_HIMALAYA = r'''
import base64
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime

UTC = timezone.utc
RESERVED = ("fail", "_sent")
CALL = {"argv": sys.argv, "cwd": os.getcwd(), "stdin_closed": None, "stdin": None}
STATE = {"json": False, "account": None}


# --- output and the call log -------------------------------------------------------------------


def finish(code):
    CALL["exit"] = code
    append_log(LOG, json.dumps(CALL) + "\n")
    sys.stdout.flush()
    sys.stdout.buffer.flush()
    sys.exit(code)


def emit(data):
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.buffer.write(text.encode("utf-8"))


def fail(message, sources=(), code=1):
    if STATE["json"]:
        emit({"error": message, "sources": list(sources), "backtrace": None})
    else:  # the layout of the real one: Error, Caused by, Suggestions
        lines = [f"Error: {message}", ""]
        if sources:
            lines += ["Caused by:", *[f" - {source}" for source in sources], ""]
        lines += ["Suggestions:", " - Run with --log-level to enable more verbose logs"]
        sys.stdout.buffer.write(("\n".join(lines) + "\n").encode("utf-8"))
    finish(code)


def refuse(entry):
    """Fail with a `fail` entry of the server file: a text, or [error, source, ...]."""
    if isinstance(entry, list):
        fail(entry[0], entry[1:])
    fail(entry)


def check_failure(server, backend):
    entry = failure(server, backend)
    if entry:
        refuse(entry)


# --- the server file ---------------------------------------------------------------------------


def load():
    with open(SERVER, encoding="utf-8") as stream:
        return json.load(stream)


def save(server):
    temp = SERVER + ".tmp"
    with open(temp, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(server, stream, indent=1)  # ASCII only, so lone surrogates survive
    os.replace(temp, SERVER)


def failure(server, backend):
    entry = (server.get("fail") or {}).get(STATE["account"])
    return entry.get(backend) if isinstance(entry, dict) else entry


def folders_of(server):
    account = STATE["account"]
    known = [name for name in server if name not in RESERVED]
    if account in RESERVED or (account not in known and account not in (server.get("fail") or {})):
        fail(f"Account `{account}` not found, the configuration holds: {', '.join(known)}")
    return server.get(account) or {}


def folder_of(server, name, missing="NONEXISTENT"):
    folders = folders_of(server)
    if name not in folders:
        fail(f"imap: NO [{missing}] Mailbox does not exist: {name}")
    return folders[name]


def uids(folder):
    return [m["uid"] for m in folder["messages"]]


def next_uid(folder):
    return max(int(folder.get("uidnext", 1)), max(uids(folder), default=0) + 1)


def put(folder, message):
    message["uid"] = next_uid(folder)
    folder["messages"].append(message)
    if "uidnext" in folder:
        folder["uidnext"] = message["uid"] + 1
    return message["uid"]


# --- messages ----------------------------------------------------------------------------------


def raw_bytes(m):
    if "raw_b64" in m:
        return base64.b64decode(m["raw_b64"])
    return m.get("raw", "").encode("utf-8", "surrogateescape")


def parsed(m):
    return message_from_bytes(raw_bytes(m), policy=policy.default)


def to_dt(text):
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None


def header_date(m):
    return to_dt(m.get("date")) or to_dt(str(parsed(m)["Date"] or ""))


def received(m):
    return to_dt(m.get("internal_date")) or header_date(m)


def iso(when):
    return when.isoformat() if when else None


def utc_key(when):
    if when is None:
        return datetime.min.replace(tzinfo=UTC)
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def header_text(msg, name):
    value = msg[name]
    return "" if value is None else str(value)


def addresses(msg, name):
    return [
        {"name": a.display_name or None, "email": a.addr_spec}
        for a in getattr(msg[name], "addresses", ())]


def body_text(msg):
    texts = []
    for part in msg.walk():
        if part.get_content_maintype() == "text":
            try:
                texts.append(part.get_content())
            except Exception:
                texts.append((part.get_payload(decode=True) or b"").decode("utf-8", "replace"))
    return "\n".join(texts)


def flag_names(m):
    return (["seen"] if m.get("seen") else []) + list(m.get("flags") or [])


def ids_of(header):
    return [i.strip("<>") for i in str(header or "").split()]


def envelope_json(m, with_attachment):
    msg = parsed(m)
    message_id = str(msg["Message-ID"] or "").strip().strip("<>") or None
    return {
        "id": str(m["uid"]),
        "message-id": message_id,
        "in-reply-to": ids_of(msg["In-Reply-To"]),
        "flags": [{"raw": "\\" + f.capitalize(), "iana": f} for f in flag_names(m)],
        "subject": header_text(msg, "Subject"),
        "from": addresses(msg, "From"),
        "to": addresses(msg, "To"),
        "date": iso(header_date(m)),
        "size": len(raw_bytes(m)),
        "has-attachment": any(True for _ in msg.iter_attachments()) if with_attachment else None,
    }


def envelope_view(m):
    msg = parsed(m)

    def shown(name):
        return [str(a) for a in getattr(msg[name], "addresses", ())]

    return {
        "date": str(msg["Date"] or "") or None, "subject": header_text(msg, "Subject") or None,
        "from": shown("From"), "sender": shown("Sender"), "reply_to": shown("Reply-To"),
        "to": shown("To"), "cc": shown("Cc"), "bcc": shown("Bcc"),
        "message_id": str(msg["Message-ID"] or "") or None,
        "in_reply_to": str(msg["In-Reply-To"] or "") or None}


# --- the query language of `envelope search` ---------------------------------------------------

TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))')
KEYWORDS = ("date", "after", "from", "to", "subject", "body", "flag")
FLAGS = ("seen", "answered", "flagged", "draft")


def tokenize(text):
    tokens = []
    text = text.rstrip()
    pos = 0
    while pos < len(text):
        found = TOKEN.match(text, pos)
        if not found:
            fail("invalid query near: " + text[pos:pos + 20])
        if found.group(1):
            tokens.append(("(", "("))
        elif found.group(2):
            tokens.append((")", ")"))
        elif found.group(3) is not None:
            tokens.append(("str", re.sub(r"\\(.)", r"\1", found.group(3))))
        else:
            tokens.append(("word", found.group(4)))
        pos = found.end()
    return tokens


def day_of(text):
    try:
        return date.fromisoformat(text)
    except ValueError:
        fail(f"invalid query: {text} is not a date (yyyy-mm-dd)")


class Query:
    def __init__(self, tokens):
        self.tokens = tokens
        self.at = 0

    def peek(self):
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def is_word(self, name):
        token = self.peek()
        return token is not None and token[0] == "word" and token[1].lower() == name

    def parse(self):
        node = self.disjunction()
        if self.at != len(self.tokens):
            fail("invalid query: unexpected " + self.tokens[self.at][1])
        return node

    def disjunction(self):
        node = self.conjunction()
        while self.is_word("or"):
            self.at += 1
            node = ("or", node, self.conjunction())
        return node

    def conjunction(self):
        node = self.negation()
        while self.is_word("and"):
            self.at += 1
            node = ("and", node, self.negation())
        return node

    def negation(self):
        if self.is_word("not"):
            self.at += 1
            return ("not", self.negation())
        return self.atom()

    def atom(self):
        token = self.peek()
        if token is None:
            fail("invalid query: it ends too early")
        if token[0] == "(":
            self.at += 1
            node = self.disjunction()
            if self.peek() is None or self.peek()[0] != ")":
                fail("invalid query: a ) is missing")
            self.at += 1
            return node
        if token[0] != "word" or token[1].lower() not in KEYWORDS:
            fail(f"invalid query: unknown keyword {token[1]}")
        kind = token[1].lower()
        self.at += 1
        value = self.peek()
        if value is None or value[0] not in ("str", "word"):
            fail(f"invalid query: {kind} needs a value")
        self.at += 1
        if kind in ("date", "after"):
            return (kind, day_of(value[1]))
        if kind == "flag" and value[1].lower() not in FLAGS:
            fail(f"invalid query: unknown flag {value[1]}")
        return (kind, value[1])


HEADERS = {"from": "From", "to": "To", "subject": "Subject"}


def matches(node, m):
    kind = node[0]
    if kind == "and":
        return matches(node[1], m) and matches(node[2], m)
    if kind == "or":
        return matches(node[1], m) or matches(node[2], m)
    if kind == "not":
        return not matches(node[1], m)
    if kind in ("date", "after"):
        when = header_date(m)
        if when is None:
            return False
        return when.date() == node[1] if kind == "date" else when.date() > node[1]
    if kind == "flag":
        return node[1].lower() in flag_names(m)
    needle = node[1].lower()
    if kind == "body":
        return needle in body_text(parsed(m)).lower()
    return needle in header_text(parsed(m), HEADERS[kind]).lower()


# --- the commands ------------------------------------------------------------------------------

VALUED = {"-c": None, "--config": None, "-b": None, "--backend": None, "--log-level": None,
          "--log-file": None, "-a": "account", "--account": "account"}


def parse_options(rest, valued, flags, query=False):
    found = {}
    positional = []
    at = 0
    while at < len(rest):
        word = rest[at]
        if query and positional:
            positional.append(word)
        elif word == "--json":
            STATE["json"] = True
        elif word in VALUED or word in valued:
            if at + 1 >= len(rest):
                fail(f"fake himalaya: {word} needs a value", code=2)
            name = VALUED[word] if word in VALUED else valued[word]
            value = rest[at + 1]
            at += 1
            if name == "account":
                STATE["account"] = value
            elif name == "flag":
                found.setdefault("flag", []).append(value)
            elif name:
                found[name] = value
        elif word in flags:
            found[flags[word]] = True
        elif word.startswith("-") and len(word) > 1:
            fail(f"fake himalaya does not know the option {word}", code=2)
        else:
            positional.append(word)
        at += 1
    return found, positional


def mailbox_list(opts, positional):
    server = load()
    check_failure(server, "imap")
    out = []
    for name, folder in folders_of(server).items():
        messages = folder["messages"]
        counts = opts.get("counts")
        out.append({
            "id": name, "name": name, "role": "inbox" if name.upper() == "INBOX" else None,
            "total": len(messages) if counts else None,
            "unread": sum(1 for m in messages if not m.get("seen")) if counts else None})
    emit({"mailboxes": out})


def envelope_search(opts, positional):
    server = load()
    check_failure(server, "imap")
    folder = folder_of(server, opts.get("mailbox", "INBOX"))
    tokens = tokenize(" ".join(positional))
    order = None
    for at in range(len(tokens) - 1):
        first, second = tokens[at], tokens[at + 1]
        if first[0] == "word" and first[1].lower() == "order" and second[1].lower() == "by":
            tokens, order = tokens[:at], tokens[at + 2:]
            break
    node = Query(tokens).parse() if tokens else None
    found = [m for m in folder["messages"] if node is None or matches(node, m)]
    found.sort(key=lambda m: m["uid"])
    if order:
        key = order[0][1].lower()
        keys = {
            "date": lambda m: utc_key(header_date(m)),
            "from": lambda m: header_text(parsed(m), "From").lower(),
            "to": lambda m: header_text(parsed(m), "To").lower(),
            "subject": lambda m: header_text(parsed(m), "Subject").lower()}
        if key not in keys:
            fail(f"invalid query: cannot order by {key}")
        found.sort(key=keys[key], reverse=len(order) > 1 and order[1][1].lower() == "desc")
    size, page = int(opts.get("size", 25)), int(opts.get("page", 1))
    window = found[(page - 1) * size:page * size]
    emit({"queued": 0, "envelopes": [envelope_json(m, opts.get("attachment")) for m in window]})


def imap_status(opts, positional):
    server = load()
    check_failure(server, "imap")
    folder = folder_of(server, positional[0])
    messages = folder["messages"]
    emit({
        "messages": len(messages), "recent": 0, "uid_next": next_uid(folder),
        "uid_validity": int(folder.get("uidvalidity", "1")),
        "unseen": sum(1 for m in messages if not m.get("seen"))})


def sequence_set(text, top):
    ranges = []
    for part in text.split(","):
        low, _, high = part.partition(":")
        try:
            ends = [top if x == "*" else int(x) for x in (low, high or low)]
        except ValueError:
            fail(f"invalid sequence set: {text}")
        ranges.append((min(ends), max(ends)))
    return ranges


def imap_fetch(opts, positional):
    server = load()
    check_failure(server, "imap")
    folder = folder_of(server, opts.get("mailbox", "INBOX"))
    if len(positional) != 1:
        fail("fake himalaya: imap fetch wants one sequence set", code=2)
    messages = folder["messages"]
    by_seq = bool(opts.get("seq"))
    top = len(messages) if by_seq else max(uids(folder), default=0)
    ranges = sequence_set(positional[0], top)
    chosen = [name for name in ("envelope", "structure", "flags", "internal_date", "size", "body")
              if opts.get(name)] or ["envelope"]
    out = []
    for position, m in enumerate(messages, 1):
        number = position if by_seq else m["uid"]
        if not any(low <= number <= high for low, high in ranges):
            continue
        if "body" in chosen and m.get("fail_fetch"):
            refuse(m["fail_fetch"])
        item = {"seq": position, "uid": m["uid"]}
        if "internal_date" in chosen:
            item["internal_date"] = iso(received(m))
        if "body" in chosen:
            item["body"] = base64.b64encode(raw_bytes(m)).decode("ascii")
        if "flags" in chosen:
            item["flags"] = ["\\" + f.capitalize() for f in flag_names(m)]
        if "size" in chosen:
            item["size"] = len(raw_bytes(m))
        if "envelope" in chosen:
            item["envelope"] = envelope_view(m)
        if "structure" in chosen:
            item["structure"] = {"content_type": "text/plain", "parts": []}
        out.append(item)
    emit({"messages": out})


def imap_search(opts, positional):
    server = load()
    check_failure(server, "imap")
    folder = folder_of(server, opts.get("mailbox", "INBOX"))
    out = []
    for position, m in enumerate(folder["messages"], 1):
        when = received(m)
        day = when.date() if when else None
        ok = True
        if "since" in opts:
            ok = ok and day is not None and day >= day_of(opts["since"])
        if "before" in opts:
            ok = ok and day is not None and day < day_of(opts["before"])
        if "on" in opts:
            ok = ok and day == day_of(opts["on"])
        if opts.get("seen"):
            ok = ok and bool(m.get("seen"))
        if opts.get("unseen"):
            ok = ok and not m.get("seen")
        if "from" in opts:
            ok = ok and opts["from"].lower() in header_text(parsed(m), "From").lower()
        if "subject" in opts:
            ok = ok and opts["subject"].lower() in header_text(parsed(m), "Subject").lower()
        if ok:
            out.append({"id": position if opts.get("seq") else m["uid"]})
    emit({"uid_mode": not opts.get("seq"), "ids": out})


def message_read(opts, positional):
    server = load()
    check_failure(server, "imap")
    name = opts.get("mailbox", "INBOX")
    folder = folder_of(server, name)
    if not opts.get("raw") or len(positional) != 1:
        fail("fake himalaya: only `message read <id> --raw` is supported", code=2)
    found = [m for m in folder["messages"] if str(m["uid"]) == positional[0]]
    if not found:
        fail(f"imap: no message {positional[0]} in {name}")
    data = raw_bytes(found[0])
    if STATE["json"]:
        emit(data.decode("utf-8", "replace"))
    else:
        sys.stdout.buffer.write(data)


def read_stdin():
    data = sys.stdin.buffer.read()
    CALL["stdin"] = data.decode("utf-8", "surrogateescape")
    kept = []
    for line in data.splitlines(keepends=True):  # himalaya stops at the first invalid line
        try:
            line.decode("utf-8")
        except UnicodeDecodeError:
            break
        kept.append(line)
    return b"".join(kept)


def message_send(opts, positional):
    mail = read_stdin()
    server = load()
    folders_of(server)
    check_failure(server, "smtp")
    server.setdefault("_sent", []).append(
        {"mailbox": STATE["account"], "raw": mail.decode("utf-8", "surrogateescape")})
    save(server)
    emit({"message": "Message successfully sent"})


def message_add(opts, positional):
    mail = read_stdin()
    server = load()
    check_failure(server, "imap")
    if "mailbox" not in opts:
        fail("fake himalaya: message add needs -m", code=2)
    folder = folder_of(server, opts["mailbox"], missing="TRYCREATE")
    entry = {
        "uid": 0, "internal_date": datetime.now(UTC).isoformat(),
        "seen": "seen" in opts.get("flag", []), "raw": mail.decode("utf-8", "surrogateescape")}
    entry["date"] = iso(header_date(entry))
    uid = put(folder, entry)
    save(server)
    emit({"id": str(uid), "sent": False})


def flag_add(opts, positional):
    server = load()
    check_failure(server, "imap")
    folder = folder_of(server, opts.get("mailbox", "INBOX"))
    flags = opts.get("flag", [])
    if not flags or any(f not in FLAGS for f in flags):
        fail("fake himalaya: flag add wants -f seen|answered|flagged|draft", code=2)
    wanted = {int(x) for x in positional}
    for m in folder["messages"]:
        if m["uid"] in wanted:
            for flag in flags:
                if flag == "seen":
                    m["seen"] = True
                elif flag not in m.setdefault("flags", []):
                    m["flags"].append(flag)
    save(server)
    emit({"flags": flags})


def message_move(opts, positional):
    server = load()
    check_failure(server, "imap")
    if "from" not in opts or "to" not in opts:
        fail("fake himalaya: message move needs --from and --to", code=2)
    source = folder_of(server, opts["from"])
    target = folder_of(server, opts["to"], missing="TRYCREATE")
    wanted = {int(x) for x in positional}
    moved = [m for m in source["messages"] if m["uid"] in wanted]
    if not moved:
        emit({"message": f"No message moved: no message {','.join(positional)} in {opts['from']}"})
        return
    source["messages"] = [m for m in source["messages"] if m["uid"] not in wanted]
    for m in moved:
        put(target, dict(m))
    save(server)
    emit({"message": "Message(s) successfully moved"})


def account_check(opts, positional):
    server = load()
    folders_of(server)
    backends = []
    for backend in ("imap", "smtp"):
        text = failure(server, backend)
        if isinstance(text, list):
            text = ": ".join(text)
        backends.append({"backend": backend, "ok": not text, "error": text or None})
    emit({"account": STATE["account"], "backends": backends})


# F-NODEL, with the aliases of himalaya (`msg`, `rm`), `imap close` (it expunges) and `imap raw`.
NODEL = {
    ("message", "delete"), ("message", "rm"), ("msg", "delete"), ("msg", "rm"),
    ("imap", "delete"), ("imap", "expunge"), ("imap", "close"), ("imap", "raw"),
}

COMMANDS = {
    ("mailbox", "list"): (mailbox_list, {}, {"--counts": "counts"}, False),
    ("envelope", "search"): (
        envelope_search,
        {"-m": "mailbox", "--mailbox": "mailbox", "-s": "size", "--page-size": "size",
         "-p": "page", "--page": "page"},
        {"--has-attachment": "attachment"}, True),
    ("imap", "status"): (imap_status, {}, {}, False),
    ("imap", "fetch"): (
        imap_fetch, {"-m": "mailbox", "--mailbox": "mailbox"},
        {"--envelope": "envelope", "--structure": "structure", "--flags": "flags",
         "--internal-date": "internal_date", "--size": "size", "--body": "body",
         "--seq": "seq"}, False),
    ("imap", "search"): (
        imap_search,
        {"-m": "mailbox", "--mailbox": "mailbox", "--since": "since", "--before": "before",
         "--on": "on", "--from": "from", "--subject": "subject"},
        {"--seen": "seen", "--unseen": "unseen", "--seq": "seq"}, False),
    ("message", "read"): (
        message_read, {"-m": "mailbox", "--mailbox": "mailbox"}, {"--raw": "raw"}, False),
    ("message", "send"): (message_send, {}, {"--no-save": "no_save"}, False),
    ("message", "add"): (
        message_add, {"-m": "mailbox", "--mailbox": "mailbox", "-f": "flag", "--flag": "flag"},
        {}, False),
    ("message", "move"): (
        message_move, {"-f": "from", "--from": "from", "-t": "to", "--to": "to"}, {}, False),
    ("flag", "add"): (
        flag_add, {"-m": "mailbox", "--mailbox": "mailbox", "-f": "flag", "--flag": "flag"},
        {}, False),
    ("account", "check"): (account_check, {}, {}, False),
}


def main():
    if sys.argv[1:] == ["--version"]:  # not a call on a mailbox: not logged
        version = os.environ.get("FAKE_HIMALAYA_VERSION") or "2.2.1"
        sys.stdout.write(f"himalaya v{version} +imap +smtp\n")
        sys.stdout.flush()
        sys.exit(0)
    CALL["stdin_closed"] = stdin_is_null()
    args = sys.argv[1:]
    at = 0
    while at < len(args) and args[at].startswith("-"):  # the global flags before the command
        if args[at] == "--json":
            STATE["json"] = True
            at += 1
        elif args[at] in VALUED:
            if VALUED[args[at]] == "account":
                STATE["account"] = args[at + 1]
            at += 2
        else:
            break
    rest = args[at:]
    head = rest[:2]
    if tuple(head) in NODEL or ("--seen" in rest and head != ["imap", "search"]):
        sys.stdout.write("F-NODEL\n")
        finish(99)
    spec = COMMANDS.get(tuple(head))
    if spec is None:
        fail("fake himalaya does not know the command: " + " ".join(head), code=2)
    handler, valued, flags, query = spec
    opts, positional = parse_options(rest[2:], valued, flags, query)
    handler(opts, positional)
    finish(0)


try:
    main()
except Exception as err:
    fail(f"fake himalaya crashed: {err!r}", code=97)
'''


def himalaya_body(server: Path, log: Path) -> str:
    """The fake himalaya for `make_exe`, bound to the server file and the call log."""
    return f"SERVER = {str(server)!r}\nLOG = {str(log)!r}\n{LOG_APPEND}\n{STDIN_NULL}\n{FAKE_HIMALAYA}"


@dataclass(frozen=True)
class FakeHimalaya:
    exe: Path  # the executable `himalaya`, first on PATH
    path: Path  # the server file (see FAKE_HIMALAYA)
    log: Path  # the call log

    def server(self) -> dict:
        """The server file as it is now: sends, adds, moves and flags are in it."""
        return json.loads(self.path.read_text("utf-8"))

    def write(self, server: dict) -> None:
        """Replace the server file, for a test that changes the mailbox between two commands."""
        self.path.write_text(json.dumps(server), "utf-8", newline="\n")

    def calls(self) -> list[dict]:
        """Every call so far, oldest first: argv, cwd, stdin_closed, stdin and exit."""
        if not self.log.exists():
            return []
        lines = self.log.read_text("utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]


# The fake Claude Code CLI: `claude -p`, the isolated reader. It logs every call and answers from
# the reply file named by $FAKE_CLAUDE_REPLY:
# - no file: the default for the schema of `--json-schema`: with `mails`, DEFAULT_ITEM for every
#   wrapped mail on stdin; with `answer` or `body`, a fixed text; else an empty object.
# - {"by_subject": {"<keyword>": {<item fields>}, ...}}: for every wrapped mail on stdin, the fields
#   of the first keyword found in its `Subject:` line (case-insensitive; "" matches every mail)
#   over DEFAULT_ITEM, with `index` the mail's number unless the fields set one. A mail that no
#   keyword matches gets DEFAULT_ITEM. Fields with `"refuse": true` make the model's safeguards
#   refuse the whole call, as claude prints it: REFUSAL (`is_error`, `stop_reason` "refusal"),
#   exit 1.
# - anything else: a fixed result object, printed as it is: the whole JSON that claude prints
#   (`envelope` below builds one).
# What it prints otherwise is claude's result object with `structured_output` and, in `result`,
# the same as JSON text. A wrapped mail is `<<<MAIL-<token> <n>>>` ... `<<<END MAIL-<token> <n>>>`.
# $FAKE_CLAUDE_MODE: `error` prints claude's answer when it is not logged in (`is_error`, exit 1);
# `garbage` prints text that is not JSON; `sleep` sleeps for a minute (for the timeout).
#
# The call log has one JSON line per call, written before it answers: `argv` (the whole sys.argv:
# argv[1:] is what the reader passed), `cwd`, `cwd_names` (what the working directory holds),
# `stdin`, and `env` (CC_SESSION_KEY and CC_PROJECT, when they are set). A crash adds a line
# {"crashed": "<error>"} and exits 97.
FAKE_CLAUDE = r'''
import json
import os
import re
import sys
import time

DEFAULT_ITEM = {
    "summary": "A mail.", "code": "", "amount": "", "deadline": "", "tier": "ignore",
    "category": "other", "reason": "Nothing to do.", "phishing": False}
TEXTS = {"answer": "The fake answer.", "body": "The fake reply."}
MAIL = re.compile(r"<<<MAIL-([0-9a-f]{16}) (\d+)>>>\n(.*?)\n<<<END MAIL-\1 \2>>>", re.S)
NOT_LOGGED_IN = "Not logged in · Please run /login"
REFUSAL = {
    "type": "result", "subtype": "success", "is_error": True, "stop_reason": "refusal",
    "result": "API Error: Claude Sonnet's safeguards flagged this message "
              "(https://www.anthropic.com/legal/aup)."}


def log(entry):
    append_log(LOG, json.dumps(entry) + "\n")


def emit(data):
    sys.stdout.write(json.dumps(data))
    sys.stdout.flush()


def option(name):
    args = sys.argv[1:]
    if name in args and args.index(name) + 1 < len(args):
        return args[args.index(name) + 1]
    return None


def subject_of(text):
    found = re.search(r"^Subject: (.*)$", text, re.M)
    return found.group(1) if found else ""


def items(stdin, by_subject):
    out = []
    for _token, n, text in MAIL.findall(stdin):
        subject = subject_of(text).lower()
        fields = next(
            (chosen for keyword, chosen in by_subject.items() if keyword.lower() in subject), {})
        out.append({"index": int(n), **DEFAULT_ITEM, **fields})
    return out


def main():
    stdin = sys.stdin.buffer.read().decode("utf-8")
    log({
        "argv": sys.argv, "cwd": os.getcwd(), "cwd_names": sorted(os.listdir(".")),
        "stdin": stdin,
        "env": {k: os.environ[k] for k in ("CC_SESSION_KEY", "CC_PROJECT") if k in os.environ}})
    mode = os.environ.get("FAKE_CLAUDE_MODE", "")
    if mode == "sleep":
        time.sleep(60)
    elif mode == "error":
        emit({"type": "result", "subtype": "success", "is_error": True, "result": NOT_LOGGED_IN})
        sys.exit(1)
    elif mode == "garbage":
        sys.stdout.write("this is not JSON\n")
        return
    path = os.environ.get("FAKE_CLAUDE_REPLY", "")
    reply = None
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as stream:
            reply = json.load(stream)
    if isinstance(reply, dict) and "by_subject" not in reply:
        emit(reply)
        return
    by_subject = (reply or {}).get("by_subject") or {}
    properties = json.loads(option("--json-schema") or "{}").get("properties") or {}
    if "mails" in properties:
        found = items(stdin, by_subject)
        refused = [item.pop("refuse", False) for item in found]
        if any(refused):
            emit(REFUSAL)
            sys.exit(1)
        structured = {"mails": found}
    else:
        structured = {key: text for key, text in TEXTS.items() if key in properties}
    emit({
        "type": "result", "subtype": "success", "is_error": False,
        "result": json.dumps(structured), "structured_output": structured})


try:
    main()
except Exception as err:
    log({"crashed": repr(err)})
    sys.exit(97)
'''


def claude_body(log: Path) -> str:
    """The fake claude for `make_exe`, bound to the call log."""
    return f"LOG = {str(log)!r}\n{LOG_APPEND}\n{FAKE_CLAUDE}"


def envelope(structured: dict | None, *, result: str | None = None,
             is_error: bool = False) -> dict:
    """What `claude -p --output-format json` prints: `structured_output` (left out when None) and
    `result` (by default the structured output as JSON text)."""
    answer = {"type": "result", "subtype": "success", "is_error": is_error}
    if result is None and structured is not None:
        result = json.dumps(structured)
    if result is not None:
        answer["result"] = result
    if structured is not None:
        answer["structured_output"] = structured
    return answer


@dataclass(frozen=True)
class FakeClaude:
    exe: Path  # the executable `claude`, first on PATH
    reply_path: Path  # the file $FAKE_CLAUDE_REPLY names (see FAKE_CLAUDE)
    log: Path  # the call log
    env: Any  # the test's monkeypatch, for `mode`

    def reply(self, structured: dict) -> None:
        """Answer every call with this structured output."""
        self.reply_raw(envelope(structured))

    def reply_raw(self, answer: dict) -> None:
        """Answer every call with exactly this result object."""
        self.reply_path.write_text(json.dumps(answer), "utf-8")

    def by_subject(self, items: dict[str, dict]) -> None:
        """Answer each wrapped mail with the fields of the first keyword in its subject."""
        self.reply_raw({"by_subject": items})

    def mode(self, name: str) -> None:
        """`error`, `garbage`, `sleep`, or "" for none (see FAKE_CLAUDE)."""
        self.env.setenv("FAKE_CLAUDE_MODE", name)

    def entries(self) -> list[dict]:
        if not self.log.exists():
            return []
        lines = self.log.read_text("utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def calls(self) -> list[dict]:
        """Every call so far, oldest first: argv, cwd, cwd_names, stdin and env."""
        return [entry for entry in self.entries() if "argv" in entry]

    def crashes(self) -> list[str]:
        return [entry["crashed"] for entry in self.entries() if "crashed" in entry]


# The fake cc-connect 1.5.0: `cc-connect send [-p P] -s S (--stdin | --file F)`. It logs every call
# and answers success unless $FAKE_CC_CONNECT_MODE says otherwise:
# - `fail`: every call prints `boom: cc-connect is not connected` on stderr and exits 1;
# - `echo`: every call prints what it read on stdin (or the file name) on stderr and exits 1;
# - `loud`: every call prints 1000 `x` on stderr and exits 1;
# - `sleep`: every call sleeps for a minute (for the timeout).
# $FAKE_CC_CONNECT_FAIL_FROM=n makes call number n and every later one fail like `fail`.
#
# The call log has one JSON line per call, written before it answers: `argv` (the whole sys.argv:
# argv[1:] is what mx passed), `stdin` (what `--stdin` read, UTF-8 text, else null), `files` (for
# each `--file` path: the base64 of its content when the call was made, null when unreadable) and
# `exit`. A crash adds a line {"crashed": "<error>"} and exits 97. Anything but `send` exits 2.
FAKE_CC_CONNECT = r'''
import base64
import json
import os
import sys
import time

LOUD = "x" * 1000


def log(entry):
    with open(LOG, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry) + "\n")


def previous_calls():
    if not os.path.exists(LOG):
        return 0
    with open(LOG, encoding="utf-8") as stream:
        return sum(1 for line in stream if "argv" in json.loads(line))


def main():
    args = sys.argv[1:]
    entry = {"argv": sys.argv, "stdin": None, "files": {}}
    number = previous_calls() + 1
    if args[:1] != ["send"]:
        entry["exit"] = 2
        log(entry)
        sys.stderr.write("fake cc-connect: only `send` is supported\n")
        sys.exit(2)
    if "--stdin" in args:
        entry["stdin"] = sys.stdin.buffer.read().decode("utf-8")
    for at, word in enumerate(args):
        if word == "--file" and at + 1 < len(args):
            path = args[at + 1]
            try:
                with open(path, "rb") as stream:
                    entry["files"][path] = base64.b64encode(stream.read()).decode("ascii")
            except OSError:
                entry["files"][path] = None
    mode = os.environ.get("FAKE_CC_CONNECT_MODE", "")
    from_call = int(os.environ.get("FAKE_CC_CONNECT_FAIL_FROM", "0") or "0")
    if mode == "sleep":
        log({**entry, "exit": None})
        time.sleep(60)
    if mode == "echo":
        entry["exit"] = 1
        log(entry)
        sys.stderr.write("cannot send: " + (entry["stdin"] or " ".join(entry["files"])) + "\n")
        sys.exit(1)
    if mode == "loud":
        entry["exit"] = 1
        log(entry)
        sys.stderr.write(LOUD + "\n")
        sys.exit(1)
    if mode == "fail" or (from_call and number >= from_call):
        entry["exit"] = 1
        log(entry)
        sys.stderr.write("boom: cc-connect is not connected\n")
        sys.exit(1)
    entry["exit"] = 0
    log(entry)


try:
    main()
except SystemExit:
    raise
except Exception as err:
    log({"crashed": repr(err)})
    sys.exit(97)
'''


def cc_connect_body(log: Path) -> str:
    """The fake cc-connect for `make_exe`, bound to the call log."""
    return f"LOG = {str(log)!r}\n{FAKE_CC_CONNECT}"


@dataclass(frozen=True)
class FakeChat:
    exe: Path  # the executable `cc-connect`, first on PATH
    log: Path  # the call log
    env: Any  # the test's monkeypatch, for `mode` and `fail_from`

    def mode(self, name: str) -> None:
        """`fail`, `echo`, `loud`, `sleep`, or "" for none (see FAKE_CC_CONNECT)."""
        self.env.setenv("FAKE_CC_CONNECT_MODE", name)

    def fail_from(self, number: int) -> None:
        """Call number `number` (from 1) and every later one fail."""
        self.env.setenv("FAKE_CC_CONNECT_FAIL_FROM", str(number))

    def entries(self) -> list[dict]:
        if not self.log.exists():
            return []
        lines = self.log.read_text("utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def calls(self) -> list[dict]:
        """Every call so far, oldest first: argv, stdin, files and exit."""
        return [entry for entry in self.entries() if "argv" in entry]

    def messages(self) -> list[str]:
        """The texts that were delivered: stdin of each `--stdin` call that exited 0."""
        return [c["stdin"] for c in self.calls() if c["stdin"] is not None and c["exit"] == 0]

    def crashes(self) -> list[str]:
        return [entry["crashed"] for entry in self.entries() if "crashed" in entry]


# The fake carillon v0.2.0: `carillon -c <config> --log-level <level> watch`. It logs its call as
# one JSON line (`argv`: the whole sys.argv, so argv[1:] is what mx passed; `cwd`; `stdin_closed`),
# prints `fake carillon: out` on stdout and `fake carillon: err` on stderr, and then, by
# $FAKE_CARILLON_MODE:
# - "" (the default): reads the config named by `-c` (relative to its working directory, as
#   carillon does), runs the `imap.hook.on-message-added.cmd` of every account once, as carillon
#   does for a new mail (with the environment variables `id`, `mailbox` and `subject`, and stdin,
#   stdout and stderr its own), logs {"hook": <account>, "exit": <status>} for each, and waits to
#   be stopped. SIGTERM logs {"stopped": "SIGTERM"} and exits 0, as carillon does.
# - "exit": exits 1 at once, as carillon does when it rejects its config.
# A crash adds {"crashed": "<error>"} and exits 97. `carillon --version` prints
# `carillon v<$FAKE_CARILLON_VERSION, default 0.2.0>` and is not logged.
FAKE_CARILLON = r'''
import json
import os
import signal
import subprocess
import sys
import time
import tomllib


def log(entry):
    append_log(LOG, json.dumps(entry) + "\n")


def stop(signum, frame):
    log({"stopped": "SIGTERM"})
    sys.exit(0)


def main():
    args = sys.argv[1:]
    if args == ["--version"]:  # not a run: not logged
        print("carillon v" + (os.environ.get("FAKE_CARILLON_VERSION") or "0.2.0"), flush=True)
        sys.exit(0)
    log({"argv": sys.argv, "cwd": os.getcwd(), "stdin_closed": stdin_is_null()})
    print("fake carillon: out", flush=True)
    print("fake carillon: err", file=sys.stderr, flush=True)
    if os.environ.get("FAKE_CARILLON_MODE") == "exit":
        sys.exit(1)
    signal.signal(signal.SIGTERM, stop)
    with open(args[args.index("-c") + 1], "rb") as stream:
        config = tomllib.load(stream)
    for name, account in config["accounts"].items():
        imap = account["imap"]
        env = {**os.environ, "id": "1", "mailbox": imap["mailbox"], "subject": "Hello"}
        done = subprocess.run(imap["hook"]["on-message-added"]["cmd"], env=env)
        log({"hook": name, "exit": done.returncode})
    while True:
        time.sleep(0.1)


try:
    main()
except SystemExit:
    raise
except Exception as err:
    log({"crashed": repr(err)})
    sys.exit(97)
'''


def carillon_body(log: Path) -> str:
    """The fake carillon for `make_exe`, bound to the call log."""
    return f"LOG = {str(log)!r}\n{LOG_APPEND}\n{STDIN_NULL}\n{FAKE_CARILLON}"


@dataclass(frozen=True)
class FakeCarillon:
    exe: Path  # the executable `carillon`, first on PATH
    log: Path  # the call log

    def entries(self) -> list[dict]:
        """Every line of the log so far: calls, hooks run, stops and crashes."""
        if not self.log.exists():
            return []
        lines = self.log.read_text("utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]


# A fake `uv` for the hook command of the generated carillon config: `uv run <script> <args>`
# runs the script with the Python of the tests, so mx's `hook` runs as carillon starts it, but
# without uv making an environment for the script (which could download).
FAKE_UV = r'''
import subprocess
import sys

args = sys.argv[1:]
if args[:1] != ["run"] or len(args) < 2:
    sys.stderr.write("fake uv: only `uv run <script> ...` is supported\n")
    sys.exit(2)
sys.exit(subprocess.call([sys.executable, *args[1:]]))
'''

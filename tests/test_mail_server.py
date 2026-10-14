"""The IMAP/SMTP adapter.

Three servers stand in for the real ones:

- `Wire`: a scripted IMAP server on 127.0.0.1, just enough of RFC 3501 (with ID, MOVE and
  UIDPLUS) for ImapSmtpServer. It logs every command, so the tests can check what goes over the
  wire: ID before the inbox is opened, EXAMINE and BODY.PEEK for reads, no EXPUNGE when flags
  change. Runs everywhere.
- aiosmtpd's Controller for SMTP. Runs everywhere.
- GreenMail (`imapserver` marker), where MAIL_TEST_IMAP=host:port is set:
  docker run -p 3143:3143 -p 3025:3025 greenmail/standalone:2.1.3
"""

from __future__ import annotations

import imaplib
import os
import re
import smtplib
import socket
import socketserver
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from uuid import uuid4

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult
from mailfakes import make_raw
from mailkit.config import Mailbox, MailError
from mailkit.message import parse
from mailkit.server import LoginError, ServerError, connect

TODAY = date(2026, 10, 14)


# -- a scripted IMAP server ------------------------------------------------------------------


@dataclass
class Message:
    raw: bytes
    flags: set[str] = field(default_factory=set)
    received: date = TODAY


@dataclass
class Folder:
    uidvalidity: int = 7
    uidnext: int = 1
    messages: dict[int, Message] = field(default_factory=dict)
    attributes: tuple[str, ...] = ()

    def add(self, message: Message) -> int:
        uid = self.uidnext
        self.uidnext += 1
        self.messages[uid] = message
        return uid


TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|\(([^()]*)\)|\x00LIT(\d+)\x00|(\S+)')
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _tokens(args: str, literals: list[bytes]) -> list:
    out = []
    for m in TOKEN.finditer(args):
        if m.group(1) is not None:
            out.append(re.sub(r"\\(.)", r"\1", m.group(1)))
        elif m.group(2) is not None:
            out.append(("list", m.group(2)))
        elif m.group(3) is not None:
            out.append(literals[int(m.group(3))])
        else:
            out.append(m.group(4))
    return out


def _imap_date(text: str) -> date:
    day, month, year = text.split("-")
    return date(int(year), MONTHS.index(month) + 1, int(day))


class Wire:
    """A scripted IMAP server for one account. `log` holds (command, arguments) per command;
    UID commands are logged as "UID FETCH" and so on."""

    def __init__(self, *, user="me@163.com", password="pw",
                 capabilities=("IMAP4rev1", "ID", "MOVE", "UIDPLUS"),
                 auth_capabilities=None, require_id=False, send_uidnext=True,
                 uid_after_body=False, fail=()):
        self.user, self.password = user, password
        self.capabilities = tuple(capabilities)
        self.auth_capabilities = tuple(auth_capabilities or capabilities)
        self.require_id = require_id
        self.send_uidnext = send_uidnext
        self.uid_after_body = uid_after_body
        self.fail = set(fail)  # commands answered with NO
        self.folders = {"INBOX": Folder()}
        self.log: list[tuple[str, str]] = []
        wire = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    wire.serve(self.rfile, self.wfile)
                except OSError:  # the client went away (or spoke TLS to this plain server)
                    pass

        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    @property
    def inbox(self) -> Folder:
        return self.folders["INBOX"]

    def commands(self) -> list[str]:
        return [command for command, _ in self.log]

    def mailbox(self, provider="qq", password=None) -> Mailbox:
        return Mailbox(name="test", address=self.user, provider=provider,
                       imap=f"127.0.0.1:{self.port}", smtp="127.0.0.1:1",
                       added="2026-10-14T10:00:00+08:00")

    # -- the protocol --

    def serve(self, rfile, wfile):
        def send(*lines):
            for line in lines:
                wfile.write(line if isinstance(line, bytes) else line.encode("utf-8"))
                if not (isinstance(line, bytes) and line.endswith(b"\r\n")):
                    wfile.write(b"\r\n")

        state = {"auth": False, "id": False, "selected": None, "readonly": True}
        send("* OK [CAPABILITY " + " ".join(self.capabilities) + "] fake ready")
        while True:
            line = rfile.readline()
            if not line:
                return
            line = line.rstrip(b"\r\n")
            literals = []
            while m := re.search(rb"\{(\d+)\+?\}$", line):
                send("+ go ahead")
                literals.append(rfile.read(int(m.group(1))))
                rest = rfile.readline().rstrip(b"\r\n")
                line = line[: m.start()] + b"\x00LIT%d\x00" % (len(literals) - 1) + rest
            tag, _, rest = line.decode("utf-8", "surrogateescape").partition(" ")
            command, _, args = rest.partition(" ")
            command = command.upper()
            if command == "UID":
                sub, _, args = args.partition(" ")
                command = f"UID {sub.upper()}"
            self.log.append((command, args))
            if command in self.fail:
                send(f"{tag} NO {command} failed on purpose")
                continue
            if self.step(command, _tokens(args, literals), tag, state, send) == "bye":
                return

    def _uids(self, spec: str, folder: Folder) -> list[int]:
        top = max(folder.messages, default=0)
        found = set()
        for part in spec.split(","):
            a, _, b = part.partition(":")
            lo = top if a == "*" else int(a)
            hi = lo if not b else (top if b == "*" else int(b))
            lo, hi = min(lo, hi), max(lo, hi)
            found |= {uid for uid in folder.messages if lo <= uid <= hi}
        return sorted(found)

    def step(self, command, toks, tag, state, send):
        caps = self.auth_capabilities if state["auth"] else self.capabilities
        selected = self.folders.get(state["selected"]) if state["selected"] else None

        def ok(text="done"):
            send(f"{tag} OK {text}")

        if command == "CAPABILITY":
            send("* CAPABILITY " + " ".join(caps))
            return ok()
        if command == "NOOP":
            return ok()
        if command == "LOGOUT":
            send("* BYE logging out")
            ok()
            return "bye"
        if command == "LOGIN":
            if toks[:2] == [self.user, self.password]:
                state["auth"] = True
                return ok("LOGIN completed")
            return send(f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials")
        if command == "ID":
            if "ID" not in caps:
                return send(f"{tag} BAD unknown command")
            state["id"] = True
            send('* ID ("name" "wire")')
            return ok()
        if not state["auth"]:
            return send(f"{tag} BAD log in first")
        if command in ("SELECT", "EXAMINE"):
            if self.require_id and not state["id"]:
                return send(f"{tag} NO SELECT Unsafe Login. Please contact kefu@188.com for help")
            folder = self.folders.get(toks[0])
            if folder is None:
                return send(f"{tag} NO no such folder")
            state["selected"], state["readonly"] = toks[0], command == "EXAMINE"
            send(f"* {len(folder.messages)} EXISTS", "* 0 RECENT",
                 f"* OK [UIDVALIDITY {folder.uidvalidity}] UIDs valid")
            if self.send_uidnext:
                send(f"* OK [UIDNEXT {folder.uidnext}] next")
            send("* FLAGS (\\Seen \\Deleted \\Flagged \\Answered \\Draft)")
            return ok("[READ-ONLY] EXAMINE done" if command == "EXAMINE" else "[READ-WRITE] done")
        if command == "STATUS":
            folder = self.folders[toks[0]]
            send(f'* STATUS "{toks[0]}" (UIDVALIDITY {folder.uidvalidity} '
                 f"UIDNEXT {folder.uidnext} MESSAGES {len(folder.messages)})")
            return ok()
        if command == "LIST":
            for name, folder in self.folders.items():
                send(f'* LIST ({" ".join(folder.attributes)}) "/" "{name}"')
            return ok()
        if command == "CREATE":
            if toks[0] in self.folders:
                return send(f"{tag} NO [ALREADYEXISTS] exists")
            self.folders[toks[0]] = Folder(uidvalidity=99)
            return ok()
        if command == "APPEND":
            folder = self.folders.get(toks[0])
            if folder is None:
                return send(f"{tag} NO [TRYCREATE] no such folder")
            flags = next((set(t[1].split()) for t in toks[1:] if isinstance(t, tuple)), set())
            uid = folder.add(Message(toks[-1], flags))
            return ok(f"[APPENDUID {folder.uidvalidity} {uid}] APPEND done")
        if command in ("CLOSE", "UNSELECT"):
            state["selected"] = None
            return ok()
        if selected is None:
            return send(f"{tag} BAD select a folder first")
        writes = ("UID STORE", "UID MOVE", "UID EXPUNGE", "EXPUNGE")
        if command in writes and state["readonly"]:
            return send(f"{tag} NO [READ-ONLY] the folder is read-only")
        if command == "UID SEARCH":
            return self.search(toks, selected, send, ok, tag)
        if command == "UID FETCH":
            uids = self._uids(toks[0], selected)
            items = toks[1][1] if isinstance(toks[1], tuple) else toks[1]
            seq = sorted(selected.messages)
            for uid in uids:
                message = selected.messages[uid]
                if "BODY[]" in items and "PEEK" not in items:
                    message.flags.add("\\Seen")
                n = seq.index(uid) + 1
                head = f"* {n} FETCH (BODY[] {{{len(message.raw)}}}\r\n".encode() \
                    if self.uid_after_body else \
                    f"* {n} FETCH (UID {uid} BODY[] {{{len(message.raw)}}}\r\n".encode()
                tail = f" UID {uid})\r\n".encode() if self.uid_after_body else b")\r\n"
                send(head + message.raw + tail)
            return ok()
        if command == "UID STORE":
            uids = self._uids(toks[0], selected)
            op, flags = toks[1].upper(), set(toks[2][1].split())
            for uid in uids:
                message = selected.messages[uid]
                if op.startswith("+"):
                    message.flags |= flags
                elif op.startswith("-"):
                    message.flags -= flags
                else:
                    message.flags = set(flags)
            return ok()
        if command in ("UID COPY", "UID MOVE"):
            if command == "UID MOVE" and "MOVE" not in caps:
                return send(f"{tag} BAD unknown command")
            target = self.folders.get(toks[1])
            if target is None:
                return send(f"{tag} NO [TRYCREATE] no such folder")
            for uid in self._uids(toks[0], selected):
                message = selected.messages[uid]
                target.add(Message(message.raw, set(message.flags) - {"\\Deleted"},
                                   message.received))
                if command == "UID MOVE":
                    del selected.messages[uid]
            return ok()
        if command == "UID EXPUNGE":
            if "UIDPLUS" not in caps:
                return send(f"{tag} BAD unknown command")
            for uid in self._uids(toks[0], selected):
                if "\\Deleted" in selected.messages[uid].flags:
                    del selected.messages[uid]
            return ok()
        if command == "EXPUNGE":
            for uid in [u for u, m in selected.messages.items() if "\\Deleted" in m.flags]:
                del selected.messages[uid]
            return ok()
        return send(f"{tag} BAD unknown command {command}")

    def search(self, toks, folder, send, ok, tag):
        found = set(folder.messages)
        i = 0
        while i < len(toks):
            word = toks[i].upper() if isinstance(toks[i], str) else toks[i]
            if word == "CHARSET":
                i += 2
            elif word == "ALL":
                i += 1
            elif word == "UID":
                found &= set(self._uids(toks[i + 1], folder))
                i += 2
            elif word == "SINCE":
                since = _imap_date(toks[i + 1])
                found &= {u for u, m in folder.messages.items() if m.received >= since}
                i += 2
            elif word == "TEXT":
                needle = toks[i + 1]
                needle = needle.decode("utf-8") if isinstance(needle, bytes) else needle
                found &= {
                    u for u, m in folder.messages.items()
                    if needle.lower() in (parse(m.raw).subject + " " + parse(m.raw).text).lower()}
                i += 2
            else:
                return send(f"{tag} BAD cannot search {word!r}")
        send("* SEARCH" + "".join(f" {uid}" for uid in sorted(found)))
        return ok()


@pytest.fixture
def wire():
    servers = []

    def make(**options) -> Wire:
        server = Wire(**options)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.stop()


def put(wire: Wire, subject: str, *, received: date = TODAY, flags=(), body="hello") -> int:
    raw = make_raw(subject, "Boss <boss@corp.example>", wire.user, body)
    return wire.inbox.add(Message(raw, set(flags), received))


def open_plain(wire: Wire, provider="qq", password="pw", **options):
    return connect(wire.mailbox(provider), password, ssl=False, **options)


# -- logging in ------------------------------------------------------------------------------


def test_id_is_sent_before_the_inbox_is_opened_for_163(wire):
    w = wire(require_id=True)  # NetEase answers "Unsafe Login" without ID
    put(w, "one")
    srv = open_plain(w, provider="163")
    try:
        assert srv.inbox_status() == (7, 2)
    finally:
        srv.close()
    commands = w.commands()
    assert commands.index("LOGIN") < commands.index("ID") < commands.index("EXAMINE")
    assert "SELECT" not in commands
    args = dict(w.log)["ID"]
    assert args == '("name" "cc-hub-mail" "version" "0.1.0" "vendor" "hub-apps")'


def test_no_id_for_providers_that_do_not_want_it(wire):
    w = wire()
    srv = open_plain(w, provider="qq")
    srv.inbox_status()
    srv.close()
    assert "ID" not in w.commands()


def test_a_server_without_id_still_works_for_163(wire):
    w = wire(capabilities=("IMAP4rev1",))
    srv = open_plain(w, provider="163")
    assert srv.inbox_status() == (7, 1)
    srv.close()


def test_imap_login_refused_names_the_step_and_the_reply_not_the_password(wire):
    w = wire()
    srv = open_plain(w, password="Wrong-Secret")
    with pytest.raises(LoginError) as caught:
        srv.check_login()
    assert caught.value.step == "imap-login"
    assert "Invalid credentials" in str(caught.value)
    assert "Wrong-Secret" not in str(caught.value)
    srv.close()


def test_opening_the_inbox_refused_is_a_login_failure(wire):
    w = wire(require_id=True, capabilities=("IMAP4rev1",))  # wants ID but cannot take it
    srv = open_plain(w, provider="qq")
    with pytest.raises(LoginError) as caught:
        srv.check_login()
    assert caught.value.step == "imap-login" and "Unsafe Login" in str(caught.value)
    srv.close()


def test_password_with_quotes_and_backslashes(wire):
    w = wire(password='a"b\\c d')
    srv = open_plain(w, password='a"b\\c d')
    assert srv.inbox_status() == (7, 1)
    srv.close()


def test_unreachable_server_is_the_connect_step():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # nothing listens here once the socket is closed
    mb = Mailbox(name="x", address="me@qq.com", provider="qq", imap=f"127.0.0.1:{port}",
                 smtp=f"127.0.0.1:{port}", added="2026-10-14T10:00:00+08:00")
    srv = connect(mb, "pw", ssl=False, timeout=5)
    with pytest.raises(LoginError) as caught:
        srv.check_login()
    assert caught.value.step == "connect" and f"127.0.0.1:{port}" in str(caught.value)
    with pytest.raises(LoginError):
        srv.send(b"x", "me@qq.com", ["a@b.example"])
    srv.close()


def test_a_silent_server_times_out_as_the_connect_step():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    held = []
    threading.Thread(target=lambda: held.append(listener.accept()), daemon=True).start()
    mb = Mailbox(name="x", address="me@qq.com", provider="qq",
                 imap=f"127.0.0.1:{listener.getsockname()[1]}", smtp="127.0.0.1:1",
                 added="2026-10-14T10:00:00+08:00")
    started = time.monotonic()
    try:
        with pytest.raises(LoginError) as caught:
            connect(mb, "pw", ssl=False, timeout=0.5).check_login()
        assert caught.value.step == "connect"
        assert time.monotonic() - started < 10
    finally:
        for conn, _ in held:
            conn.close()
        listener.close()


def test_ssl_is_the_default(wire):
    w = wire()
    srv = connect(w.mailbox(), "pw", timeout=5)  # speaks TLS to a plain server
    with pytest.raises(LoginError) as caught:
        srv.inbox_status()
    assert caught.value.step == "connect"
    assert "LOGIN" not in w.commands()  # the password never went out in the clear
    srv.close()


def test_the_password_is_not_in_the_repr(wire):
    srv = open_plain(wire(), password="Hidden-Pw")
    assert "Hidden-Pw" not in repr(srv) and "Hidden-Pw" not in str(vars(srv).keys())
    srv.close()


# -- reading never changes anything ------------------------------------------------------------


def test_reads_examine_and_peek(wire):
    w = wire()
    old = put(w, "old", received=TODAY - timedelta(days=100), body="nothing")
    new = put(w, "new hello", body="季度报告 attached")
    srv = open_plain(w)
    try:
        assert srv.inbox_status() == (7, 3)
        assert srv.uids_after(0) == [old, new]
        assert srv.uids_after(new) == []  # "n:*" matches the last message; it is left out
        assert srv.uids_since(TODAY - timedelta(days=90)) == [new]
        got = srv.fetch([old, new])
        assert [uid for uid, _ in got] == [old, new]
        assert parse(got[1][1]).subject == "new hello"
        assert got[0][1] == w.inbox.messages[old].raw
        assert srv.search_text("HELLO", None) == [new]
        assert srv.search_text("季度报告", TODAY - timedelta(days=200)) == [new]
        assert srv.search_text("nothing like it", None) == []
    finally:
        srv.close()
    assert all(not m.flags for m in w.inbox.messages.values())
    commands = w.commands()
    assert "SELECT" not in commands and "EXAMINE" in commands
    fetches = [args for command, args in w.log if command == "UID FETCH"]
    assert fetches and all("BODY.PEEK[]" in args for args in fetches)
    assert not {"UID STORE", "UID MOVE", "UID COPY", "EXPUNGE", "UID EXPUNGE", "APPEND",
                "CREATE"} & set(commands)
    searches = [args for command, args in w.log if command == "UID SEARCH"]
    assert any("CHARSET UTF-8" in args and "TEXT \x00LIT" in args for args in searches)
    assert any('TEXT "HELLO"' in args for args in searches)
    assert any("SINCE 16-Jul-2026" in args for args in searches)


def test_fetch_with_the_uid_after_the_body_and_unknown_uids(wire):
    w = wire(uid_after_body=True)
    a, b = put(w, "a"), put(w, "b")
    srv = open_plain(w)
    assert [uid for uid, _ in srv.fetch([b, a, 99])] == [a, b]
    assert srv.fetch([]) == []
    srv.close()


def test_many_uids_are_fetched_in_chunks(wire):
    w = wire()
    uids = [put(w, f"m{i}") for i in range(120)]
    srv = open_plain(w)
    assert [uid for uid, _ in srv.fetch(uids)] == uids
    srv.close()
    assert len([c for c in w.commands() if c == "UID FETCH"]) >= 2


def test_inbox_status_without_uidnext_in_the_examine_reply(wire):
    w = wire(send_uidnext=False)
    put(w, "a")
    put(w, "b")
    srv = open_plain(w)
    assert srv.inbox_status() == (7, 3)
    srv.close()


def test_a_refused_command_after_login_is_a_server_error(wire):
    w = wire(fail={"UID FETCH"})
    uid = put(w, "a")
    srv = open_plain(w, password="pw")
    with pytest.raises(ServerError, match="failed on purpose"):
        srv.fetch([uid])
    assert srv.uids_after(0) == [uid]  # the connection is still usable
    srv.close()


def test_a_dropped_connection_is_a_server_error_and_the_next_call_reconnects(wire):
    w = wire()
    uid = put(w, "a")
    srv = open_plain(w)
    assert srv.uids_after(0) == [uid]
    srv._box.client.sock.shutdown(socket.SHUT_RDWR)  # the network goes away
    with pytest.raises(ServerError):
        srv.uids_after(0)
    assert srv.uids_after(0) == [uid]
    srv.close()


# -- changing things -------------------------------------------------------------------------


def test_set_seen_selects_read_write_and_never_expunges(wire):
    w = wire()
    a = put(w, "a")
    b = put(w, "b", flags={"\\Deleted"})  # marked deleted by another client
    srv = open_plain(w)
    srv.set_seen([a, b], True)
    assert w.inbox.messages[a].flags == {"\\Seen"}
    srv.set_seen([a], False)
    assert w.inbox.messages[a].flags == set()
    assert srv.uids_after(0) == [a, b]  # a read after a write examines again
    srv.close()
    assert b in w.inbox.messages
    commands = w.commands()
    assert "SELECT" in commands and "EXPUNGE" not in commands and "UID EXPUNGE" not in commands
    stores = [args for command, args in w.log if command == "UID STORE"]
    assert stores == [f"{a},{b} +FLAGS.SILENT (\\Seen)", f"{a} -FLAGS.SILENT (\\Seen)"]
    assert commands[-3:] == ["EXAMINE", "UID SEARCH", "LOGOUT"]


def test_move_with_the_move_command(wire):
    w = wire()
    a, b = put(w, "a"), put(w, "b")
    w.folders["Trash"] = Folder(attributes=("\\HasNoChildren", "\\Trash"))
    srv = open_plain(w)
    assert ("Trash", ("\\HasNoChildren", "\\Trash")) in srv.folders()
    srv.move([a], "Trash")
    srv.close()
    assert list(w.inbox.messages) == [b]
    assert len(w.folders["Trash"].messages) == 1
    assert "UID MOVE" in w.commands() and "UID COPY" not in w.commands()


def test_move_without_move_but_with_uidplus_expunges_only_those_uids(wire):
    w = wire(capabilities=("IMAP4rev1", "UIDPLUS"))
    a = put(w, "a")
    b = put(w, "b", flags={"\\Deleted"})  # someone else's deletion stays theirs to expunge
    w.folders["Archive"] = Folder()
    srv = open_plain(w)
    srv.move([a], "Archive")
    srv.close()
    assert list(w.inbox.messages) == [b]
    assert w.folders["Archive"].messages[1].flags == set()
    assert "EXPUNGE" not in w.commands()
    assert [c for c in w.commands() if c.startswith("UID ")][-3:] == [
        "UID COPY", "UID STORE", "UID EXPUNGE"]


def test_move_without_move_or_uidplus_only_marks_deleted(wire):
    w = wire(capabilities=("IMAP4rev1",))
    a = put(w, "a")
    w.folders["Archive"] = Folder()
    srv = open_plain(w)
    srv.move([a], "Archive")
    srv.close()
    assert w.inbox.messages[a].flags == {"\\Deleted"}
    assert len(w.folders["Archive"].messages) == 1
    assert "EXPUNGE" not in w.commands() and "UID EXPUNGE" not in w.commands()


def test_move_capability_announced_only_after_login(wire):
    w = wire(capabilities=("IMAP4rev1",), auth_capabilities=("IMAP4rev1", "MOVE"))
    a = put(w, "a")
    w.folders["Trash"] = Folder()
    srv = open_plain(w)
    srv.move([a], "Trash")
    srv.close()
    assert "UID MOVE" in w.commands()


def test_move_to_a_missing_folder_is_a_server_error(wire):
    w = wire()
    a = put(w, "a")
    srv = open_plain(w)
    with pytest.raises(ServerError, match="TRYCREATE"):
        srv.move([a], "Nowhere")
    srv.close()
    assert a in w.inbox.messages


def test_create_folder_with_a_chinese_name_append_and_list(wire):
    w = wire()
    srv = open_plain(w)
    srv.create_folder("已归档")
    raw = make_raw("sent copy", w.user, "boss@corp.example", "hi")
    srv.append("已归档", raw)
    names = [name for name, _ in srv.folders()]
    with pytest.raises(ServerError):
        srv.create_folder("已归档")
    srv.close()
    assert "已归档" in names
    encoded = next(name for name in w.folders if name != "INBOX")
    assert encoded == "&XfJfUmhj-"  # modified UTF-7 on the wire
    stored = w.folders[encoded].messages[1]
    assert stored.raw == raw and stored.flags == {"\\Seen"}
    assert "SELECT" not in w.commands() and "EXAMINE" not in w.commands()


def test_nothing_to_do_sends_nothing(wire):
    w = wire()
    srv = open_plain(w)
    srv.set_seen([], True)
    srv.move([], "Trash")
    assert srv.fetch([]) == []
    srv.close()
    assert not {"SELECT", "UID STORE", "UID MOVE", "UID FETCH"} & set(w.commands())


def test_close_twice_and_context_manager(wire):
    w = wire()
    with open_plain(w) as srv:
        srv.inbox_status()
    srv.close()
    assert w.commands()[-1] == "LOGOUT"


# -- SMTP ------------------------------------------------------------------------------------


class Catcher:
    """Takes every message; refuses recipients at refused.example."""

    def __init__(self):
        self.envelopes = []

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        if address.endswith("@refused.example"):
            return "550 5.1.1 no such user"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        self.envelopes.append(envelope)
        return "250 OK queued"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def smtpd():
    started = []

    def make(user="me@163.com", password="pw"):
        def authenticator(server, session, envelope, mechanism, auth_data):
            # handled=False: aiosmtpd answers a refusal with 535 itself
            return AuthResult(success=(auth_data.login, auth_data.password) == (
                user.encode(), password.encode()), handled=False)

        catcher = Catcher()
        controller = Controller(catcher, hostname="127.0.0.1", port=free_port(),
                                authenticator=authenticator, auth_require_tls=False)
        controller.start()
        started.append(controller)
        return controller, catcher

    yield make
    for controller in started:
        controller.stop()


def full_mailbox(w: Wire, smtp_port: int, provider="qq") -> Mailbox:
    mb = w.mailbox(provider)
    mb.smtp = f"127.0.0.1:{smtp_port}"
    return mb


def test_check_login_logs_in_to_both_servers(wire, smtpd):
    w = wire()
    controller, _ = smtpd()
    srv = connect(full_mailbox(w, controller.port), "pw", ssl=False)
    srv.check_login()
    srv.close()
    assert w.commands()[:3] == ["CAPABILITY", "LOGIN", "EXAMINE"]


def test_smtp_login_refused(wire, smtpd):
    w = wire()
    controller, _ = smtpd(password="other")
    srv = connect(full_mailbox(w, controller.port), "pw", ssl=False)
    with pytest.raises(LoginError) as caught:
        srv.check_login()
    assert caught.value.step == "smtp-login" and "535" in str(caught.value)
    with pytest.raises(LoginError) as caught:
        srv.send(b"x", "me@163.com", ["a@b.example"])
    assert caught.value.step == "smtp-login"
    srv.close()


def test_send_delivers_the_raw_bytes_to_the_given_recipients(wire, smtpd):
    w = wire()
    controller, catcher = smtpd()
    raw = make_raw("报告", "me@163.com", "boss@corp.example", "正文\n.\n.line",
                   cc="c@corp.example", headers={"Bcc": "hidden@corp.example"})
    srv = connect(full_mailbox(w, controller.port), "pw", ssl=False)
    refused = srv.send(raw, "me@163.com",
                       ["boss@corp.example", "c@corp.example", "hidden@corp.example"])
    srv.close()
    assert refused == {}
    [envelope] = catcher.envelopes
    assert envelope.mail_from == "me@163.com"
    assert envelope.rcpt_tos == ["boss@corp.example", "c@corp.example", "hidden@corp.example"]
    assert envelope.content.rstrip(b"\r\n") == raw.rstrip(b"\r\n")
    assert w.log == []  # sending needs no IMAP


def test_send_failures(wire, smtpd):
    w = wire()
    controller, _ = smtpd()
    srv = connect(full_mailbox(w, controller.port), "pw", ssl=False)
    raw = b"Subject: x\r\n\r\nx\r\n"
    with pytest.raises(MailError, match="recipient"):
        srv.send(raw, "me@163.com", [])
    with pytest.raises(ServerError, match="refused every recipient.*550"):
        srv.send(raw, "me@163.com", ["a@refused.example", "b@refused.example"])
    refused = srv.send(raw, "me@163.com", ["a@refused.example", "ok@corp.example"])
    assert refused == {"a@refused.example": "550 5.1.1 no such user"}
    srv.close()


# -- GreenMail (MAIL_TEST_IMAP=host:port) ----------------------------------------------------


def _greenmail() -> tuple[str, int, str]:
    host, _, port = os.environ["MAIL_TEST_IMAP"].rpartition(":")
    smtp = os.environ.get("MAIL_TEST_SMTP") or f"{host}:3025"
    return host, int(port), smtp


@pytest.fixture
def gm():
    host, port, smtp = _greenmail()
    user = f"u{uuid4().hex[:10]}@greenmail.test"
    mb = Mailbox(name="gm", address=user, provider="qq", imap=f"{host}:{port}", smtp=smtp,
                 added="2026-10-14T10:00:00+08:00")

    def deliver(subject: str, body: str = "hello there") -> None:
        smtp_host, _, smtp_port = smtp.rpartition(":")
        raw = make_raw(subject, "sender@greenmail.test", user, body,
                       date=datetime.now().astimezone())
        with smtplib.SMTP(smtp_host, int(smtp_port), timeout=30) as s:
            s.sendmail("sender@greenmail.test", [user], raw)

    def flags(uid: int, folder: str = "INBOX") -> set[str]:
        client = imaplib.IMAP4(host, port, timeout=30)
        try:
            client.login(user, "pw")
            client.select(folder, readonly=True)
            _, data = client.uid("FETCH", str(uid), "(FLAGS)")
            text = b" ".join(d if isinstance(d, bytes) else d[0] for d in data if d).decode()
            return set(re.search(r"FLAGS \(([^)]*)\)", text).group(1).split())
        finally:
            client.logout()

    def count(folder: str) -> int:
        client = imaplib.IMAP4(host, port, timeout=30)
        try:
            client.login(user, "pw")
            _, data = client.select(f'"{folder}"', readonly=True)
            return int(data[0])
        finally:
            client.logout()

    deliver.flags, deliver.count, deliver.mb = flags, count, mb
    return deliver


def _wait_for(srv, n: int) -> list[int]:
    for _ in range(50):
        uids = srv.uids_after(0)
        if len(uids) >= n:
            return uids
        time.sleep(0.1)
    raise AssertionError(f"GreenMail did not deliver {n} messages")


@pytest.mark.imapserver
def test_greenmail_read_write_round_trip(gm):
    gm("first hello")
    gm("second")
    srv = connect(gm.mb, "pw", ssl=False)
    try:
        uids = _wait_for(srv, 2)
        uidvalidity, uidnext = srv.inbox_status()
        assert uidvalidity > 0 and uidnext > max(uids)
        assert srv.uids_after(max(uids)) == []
        assert srv.uids_after(min(uids)) == [max(uids)]
        assert srv.uids_since(date.today() - timedelta(days=1)) == uids
        fetched = srv.fetch(uids)
        assert [parse(raw).subject for _, raw in fetched] == ["first hello", "second"]
        assert "\\Seen" not in gm.flags(uids[0])  # fetching peeks
        assert srv.search_text("first", None) == [uids[0]]

        srv.set_seen([uids[0]], True)
        assert "\\Seen" in gm.flags(uids[0])
        srv.set_seen([uids[0]], False)
        assert "\\Seen" not in gm.flags(uids[0])

        srv.create_folder("Archive")
        assert "Archive" in [name for name, _ in srv.folders()]
        srv.move([uids[1]], "Archive")
        assert srv.uids_after(0) == [uids[0]]
        assert gm.count("Archive") == 1

        srv.append("Archive", make_raw("copy", gm.mb.address, "x@greenmail.test", "x"))
        assert gm.count("Archive") == 2
    finally:
        srv.close()


@pytest.mark.imapserver
def test_greenmail_with_the_163_id_command(gm):
    gm("hello")
    mb = gm.mb
    mb.provider = "163"
    srv = connect(mb, "pw", ssl=False)
    try:
        assert len(_wait_for(srv, 1)) == 1
        assert srv.inbox_status()[0] > 0
    finally:
        srv.close()


@pytest.mark.imapserver
def test_greenmail_check_login(gm):
    srv = connect(gm.mb, "pw", ssl=False)
    try:
        srv.check_login()
    finally:
        srv.close()

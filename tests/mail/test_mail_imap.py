"""The Himalaya wrapper and the mx commands against a real server: GreenMail (marker `greenmail`).

The fixture `greenmail_hub` (conftest.py) skips these unless MAIL_TEST_IMAP=host:port is set; CI
starts GreenMail in Docker on Linux, and the step "Check the greenmail tests ran" fails if any of
them skipped there. To run them locally, start GreenMail as the "Start GreenMail" step of
.github/workflows/ci.yml does, put himalaya and carillon on PATH (mx install-tool himalaya,
mx install-tool carillon) and run

    MAIL_TEST_IMAP=localhost:3143 uv run pytest -m greenmail

Every test has a fresh user, so its INBOX starts empty. Mail is put into a folder over IMAP
(`append`) when the test is not about delivery.
"""

import imaplib
import io
import json
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from uuid import uuid4

import pytest
from mailkit.cli import main
from mailkit.context import load
from mailkit.himalaya import Himalaya, HimalayaError
from mailkit.query import item
from mailkit.watch import spawn_carillon
from mailtest import GREENMAIL_BOX as BOX
from mailtest import GREENMAIL_SECRET, SmtpProxy, fix_server_name, record_chat


@pytest.fixture
def ctx(greenmail_hub):
    return load(greenmail_hub.root)


@pytest.fixture
def hima(ctx):
    return Himalaya(ctx)


def make_mail(address: str, subject: str, body: str = "Hello from the greenmail tests.") -> bytes:
    """A 7-bit mail from and to `address`, with CRLF line ends (himalaya's send needs 7-bit)."""
    msg = EmailMessage()
    msg["From"] = address
    msg["To"] = address
    msg["Subject"] = subject
    msg["Date"] = format_datetime(datetime.now(UTC))
    msg["Message-ID"] = make_msgid(domain="localhost")
    msg.set_content(body)
    raw = msg.as_bytes(policy=policy.SMTP)
    assert raw.isascii()
    return raw


@contextmanager
def imap(ctx):
    """An IMAP session of the test mailbox, for what the tests arrange or check behind himalaya."""
    box = ctx.mailbox(BOX)
    conn = imaplib.IMAP4(box.imap.host, box.imap.port, timeout=30)
    try:
        conn.login(box.address, GREENMAIL_SECRET)
        yield conn
    finally:
        with suppress(imaplib.IMAP4.error, OSError):
            conn.logout()


def append(ctx, raw: bytes, folder: str = "INBOX", when: datetime | None = None) -> None:
    """Put a mail into `folder` over IMAP, unread, received at `when` (default: now)."""
    with imap(ctx) as conn:
        answer, data = conn.append(folder, None, when, raw)
        assert answer == "OK", data


def flags_of(ctx, folder: str, uid: str | int) -> set[str]:
    """The IMAP flags of a mail, read without selecting the folder for writing."""
    with imap(ctx) as conn:
        answer, _ = conn.select(folder, readonly=True)
        assert answer == "OK"
        answer, data = conn.uid("FETCH", str(uid), "(FLAGS)")
        assert answer == "OK", data
        text = b" ".join(part for part in data if isinstance(part, bytes)).decode("ascii")
        found = re.search(r"FLAGS \(([^)]*)\)", text)
        assert found, text
        return set(found.group(1).split())


def wait_for(hima: Himalaya, folder: str, count: int, seconds: float = 10.0):
    """The envelopes of `folder` once there are `count` of them, else after `seconds` whatever is
    there (the caller's assertion then says what was missing)."""
    deadline = time.monotonic() + seconds
    while True:
        found = hima.search(BOX, folder, [], 50)
        if len(found) >= count or time.monotonic() >= deadline:
            return found
        time.sleep(0.3)


@pytest.mark.greenmail
def test_check_ok(hima):
    result = hima.check(BOX)
    assert {(backend, ok) for backend, ok, _ in result} == {("imap", True), ("smtp", True)}, result


@pytest.mark.greenmail
def test_send_then_search_then_read_keeps_unseen(ctx, hima):
    address = ctx.mailbox(BOX).address
    subject = f"gm-send-{uuid4().hex[:8]}"
    hima.send(BOX, make_mail(address, subject, "Sent through himalaya, read without a mark."))

    found = wait_for(hima, "INBOX", 1)
    assert [e.subject for e in found] == [subject], found
    envelope = found[0]
    uid = int(envelope.id)
    assert (envelope.from_addr, envelope.seen) == (address, False)

    raw = hima.raw(BOX, "INBOX", envelope.id)
    assert subject.encode() in raw and b"Sent through himalaya" in raw
    assert hima.fetch_raw(BOX, "INBOX", [uid]) == {uid: raw}

    # Reading, by either way, did not mark the mail: not for himalaya, not for the server.
    unseen = ["subject", f'"{subject}"', "and", "not", "flag", "seen"]
    assert [(e.id, e.seen) for e in hima.search(BOX, "INBOX", unseen, 10)] == [(envelope.id, False)]
    assert "\\Seen" not in flags_of(ctx, "INBOX", uid)
    # send keeps no copy (--no-save): the caller appends it to the sent folder.
    assert hima.search(BOX, "Sent", [], 10) == []


@pytest.mark.greenmail
def test_status_and_fetch_dates(ctx, hima):
    address = ctx.mailbox(BOX).address
    # Received at half past nine: GreenMail answered 00:33 for a mail appended at 12:33.
    now = datetime.now(UTC).replace(hour=9, minute=30, second=0, microsecond=0)
    old, recent = now - timedelta(days=10), now - timedelta(days=2)

    validity, first = hima.status(BOX, "INBOX")
    append(ctx, make_mail(address, "gm-old"), when=old)
    assert hima.status(BOX, "INBOX") == (validity, first + 1)
    append(ctx, make_mail(address, "gm-recent"), when=recent)
    assert hima.status(BOX, "INBOX") == (validity, first + 2)
    newest = first + 1

    # The time a mail was received, by UID; a day is the date of the received time.
    assert hima.fetch_dates(BOX, "INBOX", f"{first}:{newest}") == {first: old, newest: recent}
    assert hima.uids_since(BOX, "INBOX", (now - timedelta(days=5)).date()) == [newest]
    assert hima.uids_since(BOX, "INBOX", (now - timedelta(days=30)).date()) == [first, newest]

    # `n+1:*` is the catch-up after UID n: a server may answer with the highest mail even when
    # nothing is newer (RFC 3501 reads `*` as the largest UID in use), so the caller keeps only
    # what is above n.
    nothing = hima.fetch_dates(BOX, "INBOX", f"{newest + 1}:*")
    assert set(nothing) <= {newest}, nothing
    assert [uid for uid in nothing if uid > newest] == []
    append(ctx, make_mail(address, "gm-new"))
    assert hima.status(BOX, "INBOX") == (validity, first + 3)
    later = hima.fetch_dates(BOX, "INBOX", f"{newest + 1}:*")
    assert [uid for uid in later if uid > newest] == [newest + 1], later


@pytest.mark.greenmail
def test_move_to_trash_keeps_the_mail(ctx, hima):
    address = ctx.mailbox(BOX).address
    subject = f"gm-move-{uuid4().hex[:8]}"
    append(ctx, make_mail(address, subject, "This one goes to the trash, not away."))
    envelope = wait_for(hima, "INBOX", 1)[0]
    before = hima.raw(BOX, "INBOX", envelope.id)

    hima.move(BOX, "INBOX", envelope.id, "Trash")

    assert hima.search(BOX, "INBOX", [], 10) == []
    trashed = hima.search(BOX, "Trash", [], 10)
    assert [e.subject for e in trashed] == [subject]
    assert hima.raw(BOX, "Trash", trashed[0].id) == before
    # himalaya answers exit 0 when nothing moved; the wrapper turns its text into an error.
    with pytest.raises(HimalayaError, match="No message moved"):
        hima.move(BOX, "INBOX", envelope.id, "Trash")
    assert len(hima.search(BOX, "Trash", [], 10)) == 1


@pytest.mark.greenmail
def test_add_puts_a_seen_copy_in_sent(ctx, hima):
    address = ctx.mailbox(BOX).address
    subject = f"gm-sent-{uuid4().hex[:8]}"
    raw = make_mail(address, subject, "The copy of a mail that was sent.")

    hima.add(BOX, "Sent", raw)

    copies = hima.search(BOX, "Sent", [], 10)
    assert [(e.subject, e.seen) for e in copies] == [(subject, True)], copies
    assert "\\Seen" in flags_of(ctx, "Sent", copies[0].id)
    assert b"The copy of a mail that was sent." in hima.raw(BOX, "Sent", copies[0].id)
    assert hima.search(BOX, "INBOX", [], 10) == []


@pytest.mark.greenmail
def test_list_finds_a_chinese_subject(greenmail_hub, ctx, hima, capsys):
    """`mx list --subject 发票` sends no subject to the server (servers differ on non-ASCII
    searches) and filters the newest envelopes itself."""
    address = ctx.mailbox(BOX).address
    tag = uuid4().hex[:8]
    subject = f"发票 gm-list-{tag}"
    hima.send(BOX, make_mail(address, subject, "A mail with a Chinese subject."))
    hima.send(BOX, make_mail(address, f"gm-list-other-{tag}"))
    wait_for(hima, "INBOX", 2)

    def listed(*args: str) -> dict:
        code = main(["--hub", str(greenmail_hub.root), "list", "--mailbox", BOX, *args])
        out = capsys.readouterr()
        assert code == 0, out.err
        return json.loads(out.out)

    found = listed("--subject", "发票")
    assert [i["subject"] for i in found["items"]] == [subject], found
    assert found["errors"] == {}
    assert (found["items"][0]["n"], found["items"][0]["folder"]) == (1, "INBOX")
    assert item(ctx, 1).subject == subject  # the number resolves to that mail
    assert listed("--subject", "账单")["items"] == []
    assert len(listed()["items"]) == 2


@pytest.mark.greenmail
def test_show_and_move_on_a_real_server(greenmail_hub, ctx, hima, fake_chat, capsys):
    """Show goes to the chat and leaves the mail unread; only `mark-read` sets \\Seen; `move`
    puts the mail into the trash folder, whole, and leaves nothing in the inbox."""
    record_chat(greenmail_hub)
    address = ctx.mailbox(BOX).address
    subject = f"gm-show-{uuid4().hex[:8]}"
    append(ctx, make_mail(address, subject, "Shown in the chat, then moved to the trash."))
    assert len(wait_for(hima, "INBOX", 1)) == 1

    def run(*argv: str) -> dict:
        code = main(["--hub", str(greenmail_hub.root), *argv])
        out = capsys.readouterr()
        assert code == 0, out.err
        return json.loads(out.out)

    assert [i["subject"] for i in run("list", "--mailbox", BOX)["items"]] == [subject]
    uid = item(ctx, 1).id

    assert run("show", "1") == {"n": 1, "shown_in_chat": True}
    [text] = fake_chat.messages()
    assert text.startswith(f"From: {address}\n") and f"\nSubject: {subject}\n" in text
    assert text.endswith("\n\nShown in the chat, then moved to the trash.")
    assert "\\Seen" not in flags_of(ctx, "INBOX", uid)

    assert run("mark-read", "1") == {"marked_read": 1}
    assert "\\Seen" in flags_of(ctx, "INBOX", uid)

    original = hima.raw(BOX, "INBOX", uid)
    assert run("move", "1", "--to", "trash") == {"moved": 1, "to": "Trash"}
    assert hima.search(BOX, "INBOX", [], 10) == []
    [trashed] = hima.search(BOX, "Trash", [], 10)
    assert trashed.subject == subject
    assert hima.raw(BOX, "Trash", trashed.id) == original


def inbox_of(ctx, address: str, count: int, seconds: float = 15.0) -> list[bytes]:
    """The mails in the INBOX of another GreenMail user, as bytes, once there are `count` of
    them, else after `seconds` whatever is there. GreenMail makes a user on delivery or on the
    first login and takes any password."""
    box = ctx.mailbox(BOX)
    deadline = time.monotonic() + seconds
    while True:
        conn = imaplib.IMAP4(box.imap.host, box.imap.port, timeout=30)
        found: list[bytes] = []
        try:
            conn.login(address, GREENMAIL_SECRET)
            answer, _ = conn.select("INBOX", readonly=True)
            assert answer == "OK"
            answer, data = conn.uid("SEARCH", None, "ALL")
            assert answer == "OK", data
            for uid in data[0].split():
                answer, parts = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                assert answer == "OK", parts
                found += [part[1] for part in parts if isinstance(part, tuple)]
        finally:
            with suppress(imaplib.IMAP4.error, OSError):
                conn.logout()
        if len(found) >= count or time.monotonic() >= deadline:
            return found
        time.sleep(0.3)


CODE_AT_END = re.compile(r"(?<![0-9])([0-9]{6})$")  # the code ends the last line


def drafted_and_sent(greenmail_hub, fake_chat, capsys, monkeypatch, *argv: str) -> dict:
    """`mx draft --mailbox test <argv>` with a body on stdin, then `mx send` with the code the
    chat got: what send printed."""

    def mx(*words: str, stdin: str = "") -> dict:
        data = io.BytesIO(stdin.encode("utf-8"))
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(data, encoding="utf-8"))
        code = main(["--hub", str(greenmail_hub.root), *words])
        out = capsys.readouterr()
        assert code == 0, out.err
        return json.loads(out.out)

    body = "Hello from the send gate.\n中文正文。\n"
    draft = mx("draft", "--mailbox", BOX, *argv, stdin=body)["draft"]
    code = CODE_AT_END.search(fake_chat.messages()[-1]).group(1)
    return mx("send", draft, "--code", code)


@pytest.mark.greenmail
def test_send_and_sent_copy_on_a_real_server(greenmail_hub, ctx, hima, fake_chat, capsys,
                                             monkeypatch):
    """A draft goes out through himalaya, 7-bit, with a Chinese subject and body; the copy is
    appended to Sent, marked as read (save_copy)."""
    record_chat(greenmail_hub)
    rcpt = f"rcpt-{uuid4().hex[:8]}@greenmail.test"
    subject = f"gm-draft-{uuid4().hex[:8]} 你好"

    sent = drafted_and_sent(
        greenmail_hub, fake_chat, capsys, monkeypatch, "--to", rcpt, "--subject", subject)

    assert set(sent) == {"sent", "message_id"}, sent  # no copy_failed
    [raw] = inbox_of(ctx, rcpt, 1)
    mail = message_from_bytes(raw, policy=policy.default)
    assert mail["Subject"] == subject and mail["Message-ID"] == sent["message_id"]
    assert mail["From"] == ctx.mailbox(BOX).address and mail["To"] == rcpt
    assert "中文正文。" in mail.get_content()
    copies = hima.search(BOX, "Sent", [], 10)
    assert [(e.subject, e.seen) for e in copies] == [(subject, True)], copies
    assert "\\Seen" in flags_of(ctx, "Sent", copies[0].id)
    copy =message_from_bytes(hima.raw(BOX, "Sent", copies[0].id), policy=policy.default)
    assert copy["Message-ID"] == sent["message_id"]
    assert hima.search(BOX, "INBOX", [], 10) == []  # it went to someone else


@pytest.mark.greenmail
def test_bcc_is_stripped_on_a_real_server(greenmail_hub, ctx, hima, fake_chat, capsys,
                                          monkeypatch):
    """himalaya sends to To and Bcc and strips the Bcc line: both get the mail and
    neither copy names the hidden recipient. The sent copy keeps it, as the sender's record."""
    record_chat(greenmail_hub)
    tag = uuid4().hex[:8]
    seen, hidden = f"to-{tag}@greenmail.test", f"bcc-{tag}@greenmail.test"
    subject = f"gm-bcc-{tag}"

    drafted_and_sent(greenmail_hub, fake_chat, capsys, monkeypatch,
                     "--to", seen, "--bcc", hidden, "--subject", subject)

    [to_copy] = inbox_of(ctx, seen, 1)
    [bcc_copy] = inbox_of(ctx, hidden, 1)
    for raw in (to_copy, bcc_copy):
        assert subject.encode() in raw
        assert not re.search(rb"(?im)^bcc:", raw), raw
    assert hidden.encode() not in to_copy
    [copy] = hima.search(BOX, "Sent", [], 10)
    assert re.search(rb"(?im)^bcc: " + re.escape(hidden.encode()), hima.raw(BOX, "Sent", copy.id))


def watch_log(ctx) -> str:
    path = ctx.state / "watch.log"
    return path.read_text("utf-8", "replace") if path.exists() else "(no watch.log)"


@pytest.mark.greenmail
def test_carillon_hook_fires_on_new_mail(greenmail_hub, ctx):
    """The real carillon (CI installs it), started as `mx watch` starts it, with the carillon.toml
    that `configure` wrote (IMAP without TLS, the password sent by LOGIN): a new mail in the inbox
    runs `mx hook test`, which touches state/pokes/test; carillon stops on terminate()."""
    if shutil.which("carillon") is None:
        pytest.skip("carillon is not on PATH: mx install-tool carillon")
    address = ctx.mailbox(BOX).address
    poke = ctx.state / "pokes" / BOX
    proc = spawn_carillon(ctx)
    try:
        deadline = time.monotonic() + 30
        while not poke.exists():
            assert proc.poll() is None, f"carillon exited: {watch_log(ctx)}"
            assert time.monotonic() < deadline, f"no poke within 30 s: {watch_log(ctx)}"
            # Mail that came before carillon's IDLE began is no news to it: send until it sees one.
            append(ctx, make_mail(address, f"gm-carillon-{uuid4().hex[:8]}"))
            wait = time.monotonic() + 3
            while not poke.exists() and time.monotonic() < wait:
                time.sleep(0.1)
    finally:
        proc.terminate()
        try:
            code = proc.wait(timeout=40)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail(f"carillon did not stop on SIGTERM: {watch_log(ctx)}")
    assert code == 0, watch_log(ctx)
    assert poke.is_file()


# The proxy in front of GreenMail's SMTP port (see greenmail_hub in conftest.py). These two run
# everywhere, without GreenMail.


@pytest.mark.parametrize("line, fixed", [
    (b"220 /172.17.0.2 GreenMail SMTP Service v2.1.3 ready\r\n",
     b"220 localhost GreenMail SMTP Service v2.1.3 ready\r\n"),
    (b"250-/172.17.0.2\r\n", b"250-localhost\r\n"),
    (b"250 AUTH PLAIN LOGIN XOAUTH2\r\n", b"250 AUTH PLAIN LOGIN XOAUTH2\r\n"),
    (b"220 mail.example.org ESMTP\r\n", b"220 mail.example.org ESMTP\r\n"),
    (b"250 OK /172.17.0.2\r\n", b"250 OK /172.17.0.2\r\n"),  # only the server name changes
])
def test_fix_server_name(line, fixed):
    assert fix_server_name(line) == fixed


def test_the_smtp_proxy_changes_only_the_server_name_in_replies():
    received = []

    class Server(socketserver.StreamRequestHandler):
        def handle(self):
            self.wfile.write(b"220 /172.17.0.2 GreenMail SMTP Service v2.1.3 ready\r\n")
            received.append(self.rfile.readline())
            self.wfile.write(b"250-/172.17.0.2\r\n250 AUTH PLAIN LOGIN\r\n")

    server = socketserver.TCPServer(("127.0.0.1", 0), Server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proxy = SmtpProxy(("127.0.0.1", server.server_address[1]))
    try:
        client = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
        with client, client.makefile("rb") as replies:
            greeting = replies.readline()
            assert greeting == b"220 localhost GreenMail SMTP Service v2.1.3 ready\r\n"
            client.sendall(b"EHLO /172.17.0.2\r\n")
            assert replies.readline() == b"250-localhost\r\n"
            assert replies.readline() == b"250 AUTH PLAIN LOGIN\r\n"
    finally:
        proxy.stop()
        server.shutdown()
        server.server_close()
    assert received == [b"EHLO /172.17.0.2\r\n"]  # what the client says goes through as it is

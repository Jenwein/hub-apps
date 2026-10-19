"""The Himalaya wrapper against a real IMAP and SMTP server: GreenMail (marker `greenmail`).

The fixture `greenmail_hub` (conftest.py) skips these unless MAIL_TEST_IMAP=host:port is set; CI
starts GreenMail in Docker on Linux, and the step "Check the greenmail tests ran" fails if any of
them skipped there. To run them locally, start GreenMail as the "Start GreenMail" step of
.github/workflows/ci.yml does, put himalaya on PATH (mx install-tool himalaya) and run

    MAIL_TEST_IMAP=localhost:3143 uv run pytest -m greenmail

Every test has a fresh user, so its INBOX starts empty. Mail is put into a folder over IMAP
(`append`) when the test is not about delivery.
"""

import imaplib
import re
import socket
import socketserver
import threading
import time
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from uuid import uuid4

import pytest
from mailkit.context import load
from mailkit.himalaya import Himalaya, HimalayaError
from mailtest import GREENMAIL_BOX as BOX
from mailtest import GREENMAIL_SECRET, SmtpProxy, fix_server_name


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

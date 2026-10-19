import base64
import json
import shutil
import socket
import subprocess
import time
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fakes import himalaya_body
from mailkit import himalaya
from mailkit.cli import main
from mailkit.context import load
from mailkit.himalaya import (
    AUTH_PATTERNS,
    CONNECT_PATTERNS,
    AuthError,
    ConnectError,
    Envelope,
    Folder,
    Himalaya,
    HimalayaError,
    classify,
    decode_utf7,
    guess_folders,
    norm,
    parse_envelopes,
    parse_error,
    parse_folders,
)
from mailkit.settings import MailError
from mailtest import eml_with_attachment, write_mailboxes, write_placeholder_config

FIXTURES = Path(__file__).parent / "fixtures" / "himalaya"

BOXES = """\
[[mailbox]]
name = "qq"
address = "me@qq.com"
provider = "qq"

[[mailbox]]
name = "gmail"
address = "me@gmail.com"
provider = "gmail"
"""

SHANGHAI = timezone(timedelta(hours=8))


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text("utf-8"))


def eml(subject: str, sender: str, when: str, body: str = "hello", extra: str = "") -> str:
    return (
        f"From: {sender}\nTo: me@qq.com\nSubject: {subject}\nDate: {when}\n"
        f"Message-ID: <{abs(hash((subject, when)))}@example.org>\n{extra}"
        f"Content-Type: text/plain; charset=utf-8\n\n{body}\n")


def msg(uid: int, subject: str, sender: str = "Alice <alice@example.org>",
        when: str = "Mon, 19 Oct 2026 10:00:00 +0800", *, seen: bool = False,
        body: str = "hello", internal: str | None = None, extra: str = "") -> dict:
    return {
        "uid": uid, "date": when, "internal_date": internal or when, "seen": seen,
        "raw": eml(subject, sender, when, body, extra)}


def make_server() -> dict:
    return {
        "qq": {
            "INBOX": {"uidvalidity": "7", "messages": [
                msg(1, "invoice from Bob", "Bob <bob@example.org>",
                    "Fri, 16 Oct 2026 09:00:00 +0800", seen=True, body="the money"),
                msg(2, "lunch", "Alice <alice@example.org>",
                    "Sun, 18 Oct 2026 12:00:00 +0800", body="noodles at noon"),
                msg(5, "季度发票", "Carol <carol@example.org>",
                    "Mon, 19 Oct 2026 10:00:00 +0800", body="see the file"),
            ]},
            "Sent Messages": {"uidvalidity": "7", "messages": []},
            "Deleted Messages": {"uidvalidity": "7", "messages": []},
        },
        "gmail": {
            "INBOX": {"uidvalidity": "3", "messages": [msg(1, "hi gmail")]},
        },
    }


@pytest.fixture
def ctx(hub):
    write_mailboxes(hub, BOXES)
    context = load(hub.root)
    write_placeholder_config(hub)  # Himalaya.run wants a himalaya.toml; the fakes ignore it
    return context


def words(call: dict) -> list[str]:
    """The command words of a logged call: argv after `-c himalaya.toml -a <mailbox> [--json]`."""
    rest = call["argv"][5:]
    return rest[1:] if rest[:1] == ["--json"] else rest


def utf7(text: str) -> str:
    """The IMAP modified UTF-7 of a folder name (RFC 3501 5.1.3), made independently of the
    decoder under test."""
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            b64 = base64.b64encode("".join(run).encode("utf-16-be")).decode()
            out.append("&" + b64.rstrip("=").replace("/", ",") + "-")
            run.clear()

    for char in text:
        if " " <= char <= "~":
            flush()
            out.append("&-" if char == "&" else char)
        else:
            run.append(char)
    flush()
    return "".join(out)


# --- where and how himalaya is run -----------------------------------------------------------


def test_runs_in_the_state_dir_with_a_relative_config(hub, ctx, fake_himalaya):
    fake = fake_himalaya(make_server())
    hima = Himalaya(ctx)
    hima.folders("qq")
    hima.search("gmail", "INBOX", [], 5)
    hima.status("qq", "INBOX")
    calls = fake.calls()
    assert len(calls) == 3
    for call in calls:
        assert Path(call["cwd"]).samefile(hub.state)
        assert call["argv"][1:5] == ["-c", "himalaya.toml", "-a", call["argv"][4]]
    assert [c["argv"][4] for c in calls] == ["qq", "gmail", "qq"]


def test_stdin_is_closed(hub, ctx, fake_himalaya):
    fake = fake_himalaya(make_server())
    hima = Himalaya(ctx)
    hima.folders("qq")
    hima.search("qq", "INBOX", [], 5)
    hima.status("qq", "INBOX")
    hima.fetch_dates("qq", "INBOX", "1:*")
    hima.uids_since("qq", "INBOX", date(2026, 10, 1))
    hima.fetch_raw("qq", "INBOX", [1])
    hima.raw("qq", "INBOX", "1")
    hima.mark_seen("qq", "INBOX", "2")
    hima.move("qq", "INBOX", "2", "Deleted Messages")
    hima.check("qq")
    hima.send("qq", b"From: a@b.c\r\nTo: d@e.f\r\nSubject: x\r\n\r\nhi\r\n")
    hima.add("qq", "Sent Messages", b"From: a@b.c\r\n\r\nhi\r\n")
    calls = fake.calls()
    assert len(calls) == 12
    for call in calls[:-2]:
        assert call["stdin_closed"] is True, call["argv"]
    for call in calls[-2:]:  # send and add get the mail on a pipe
        assert call["stdin_closed"] is False
        assert call["stdin"].startswith("From: a@b.c")


def test_each_method_is_one_command(hub, ctx, fake_himalaya):
    fake = fake_himalaya(make_server())
    hima = Himalaya(ctx)
    hima.folders("qq")
    hima.search("qq", "INBOX", ["subject", '"x"', "and", "not", "flag", "seen"], 7)
    hima.status("qq", "INBOX")
    hima.fetch_dates("qq", "INBOX", "6:*")
    hima.uids_since("qq", "INBOX", date(2026, 10, 3))
    hima.fetch_raw("qq", "INBOX", [1, 5])
    hima.raw("qq", "INBOX", "5")
    hima.mark_seen("qq", "INBOX", "5")
    hima.move("qq", "INBOX", "5", "Deleted Messages")
    hima.check("qq")
    hima.send("qq", b"Subject: x\r\n\r\nhi\r\n")
    hima.add("qq", "Sent Messages", b"Subject: x\r\n\r\nhi\r\n")
    calls = fake.calls()
    assert [call["argv"][5:6] == ["--json"] for call in calls] == [True] * 6 + [False] + [True] * 5
    assert [words(call) for call in calls] == [
        ["mailbox", "list"],
        ["envelope", "search", "-m", "INBOX", "-s", "7", "-p", "1", "--has-attachment",
         "subject", '"x"', "and", "not", "flag", "seen", "order", "by", "date", "desc"],
        ["imap", "status", "INBOX"],
        ["imap", "fetch", "-m", "INBOX", "6:*", "--internal-date"],
        ["imap", "search", "-m", "INBOX", "--since", "2026-10-03"],
        ["imap", "fetch", "-m", "INBOX", "1,5", "--body"],
        ["message", "read", "5", "-m", "INBOX", "--raw"],
        ["flag", "add", "-m", "INBOX", "5", "-f", "seen"],
        ["message", "move", "5", "--from", "INBOX", "--to", "Deleted Messages"],
        ["account", "check"],
        ["message", "send", "--no-save"],
        ["message", "add", "-m", "Sent Messages", "-f", "seen"],
    ]


def test_no_method_runs_a_forbidden_command(hub, ctx, fake_himalaya):
    fake = fake_himalaya(make_server())
    hima = Himalaya(ctx)
    hima.folders("qq")
    hima.search("qq", "INBOX", [], 5)
    hima.raw("qq", "INBOX", "1")
    hima.fetch_raw("qq", "INBOX", [1])
    hima.mark_seen("qq", "INBOX", "2")
    hima.move("qq", "INBOX", "2", "Deleted Messages")
    for call in fake.calls():
        assert call["exit"] != 99
        assert "--seen" not in call["argv"]
        assert call["argv"][5:7] not in (["message", "delete"], ["imap", "expunge"])


FORBIDDEN_SPELLINGS = [
    ["message", "delete", "1"], ["message", "rm", "1"], ["msg", "delete", "1"],
    ["msg", "rm", "1"], ["imap", "expunge"], ["imap", "close"], ["imap", "delete", "Trash"],
    ["imap", "raw", "A1 EXPUNGE"], ["message", "read", "1", "-m", "INBOX", "--raw", "--seen"],
    ["Message", "Delete", "1"], ["--log-level", "off", "message", "delete", "1"],
]


def test_the_fake_refuses_what_must_never_run(hub, ctx, make_exe):
    """The fake exits 99 with F-NODEL on stdout for each spelling of F-NODEL, so a wrapper that
    ever ran one fails its test; `imap search --seen` is only a search filter and passes. The
    fake is started directly: Himalaya.run would not."""
    server, log = hub.state / "server.json", hub.state / "calls.log"
    server.write_text(json.dumps(make_server()), "utf-8")
    exe = make_exe("himalaya", himalaya_body(server, log))

    def start(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [exe, "-c", "himalaya.toml", "-a", "qq", "--json", *args], cwd=hub.state,
            stdin=subprocess.DEVNULL, capture_output=True, check=False)

    spellings = [args for args in FORBIDDEN_SPELLINGS if args[0] != "Message"]  # clap: no case
    for args in spellings:
        done = start(*args)
        assert (done.returncode, done.stdout.strip()) == (99, b"F-NODEL"), args
    assert start("imap", "search", "-m", "INBOX", "--seen").returncode == 0
    codes = [json.loads(line)["exit"] for line in log.read_text("utf-8").splitlines()]
    assert codes == [99] * len(spellings) + [0]


@pytest.mark.parametrize("args", FORBIDDEN_SPELLINGS)
def test_the_guard_refuses_each_spelling_before_anything_runs(hub, ctx, fake_himalaya, args):
    fake = fake_himalaya(make_server())
    with pytest.raises(MailError, match="never runs") as raised:
        Himalaya(ctx).run("qq", *args)
    assert type(raised.value) is MailError
    assert fake.calls() == []


def test_the_guard_leaves_a_search_filter_and_the_move_to_the_trash_alone(
        hub, ctx, fake_himalaya):
    fake = fake_himalaya(make_server())
    hima = Himalaya(ctx)
    assert hima.run("qq", "imap", "search", "-m", "INBOX", "--seen")
    hima.move("qq", "INBOX", "2", "Deleted Messages")  # "delete" is a move
    hima.search("qq", "INBOX", ["subject", '"delete"', "and", "not", "flag", "seen"], 5)
    assert [call["exit"] for call in fake.calls()] == [0, 0, 0]


def test_missing_himalaya_names_the_installer(hub, ctx, monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(MailError, match="mx install-tool himalaya"):
        Himalaya(ctx).folders("qq")
    with pytest.raises(MailError, match="mx install-tool himalaya"):
        Himalaya(ctx, exe="no-such-himalaya").folders("qq")


def test_a_machine_without_a_config_is_told_to_run_configure(hub, ctx, fake_himalaya, capsys):
    fake = fake_himalaya(make_server())
    (hub.state / "himalaya.toml").unlink()
    with pytest.raises(MailError, match="no mail config yet: run mx configure") as raised:
        Himalaya(ctx).folders("qq")
    assert type(raised.value) is MailError  # himalaya's own "run himalaya configure" is not heard
    assert fake.calls() == []  # himalaya was not called
    code, out, err = run_cli(hub, capsys, "check")
    assert (code, out) == (1, "") and "run mx configure" in err


def test_exe_overrides_the_path(hub, ctx, fake_himalaya, make_exe):
    fake_himalaya(make_server())
    other = make_exe("other-himalaya", (
        "print('{\"mailboxes\": [{\"id\": \"Z\", \"name\": \"Z\", \"role\": null}]}')"))
    assert Himalaya(ctx, exe=str(other)).folders("qq") == [Folder("Z", "Z", None)]
    assert Himalaya(ctx).folders("qq")[0].name == "INBOX"


# --- the parse layer --------------------------------------------------------------------------

EXPECTED = [
    Envelope("1042", "季度发票 Q3 invoice", "Alice 财务", "alice@example.org",
             datetime(2026, 10, 19, 10, 0, tzinfo=SHANGHAI), True, True),
    Envelope("1041", "", "", "", None, False, None),
    Envelope("1040", "Re: lunch", "", "bob@example.org",
             datetime(2026, 10, 18, 4, 30, tzinfo=UTC), False, False),
]


def test_parse_kebab_and_camel():
    kebab = parse_envelopes(load_fixture("envelopes-kebab.json"))
    camel = parse_envelopes(load_fixture("envelopes-camel.json"))
    assert kebab == camel == EXPECTED
    assert kebab[0].date.utcoffset() == timedelta(hours=8)


def test_norm_reads_the_three_spellings_alike():
    assert norm("has-attachment") == norm("has_attachment") == norm("hasAttachment")
    assert norm("uid_validity") == norm("uidValidity") == "uidvalidity"


def test_flags_may_be_plain_strings_and_seen_has_any_spelling():
    for flag in ("\\Seen", "seen", {"raw": "\\SEEN", "iana": None}, {"raw": "x", "iana": "seen"}):
        data = {"envelopes": [{"id": "1", "flags": [flag]}]}
        assert parse_envelopes(data)[0].seen is True
    data = {"envelopes": [{"id": "1", "flags": ["\\Answered", "$Seen2"]}]}
    assert parse_envelopes(data)[0].seen is False


def test_a_missing_or_odd_date_is_none():
    for value in (None, "", "not a date", 5):
        data = {"envelopes": [{"id": "9", "date": value}]}
        assert parse_envelopes(data)[0].date is None
    assert parse_envelopes({"envelopes": []}) == []


def test_parse_folders():
    data = {"mailboxes": [
        {"id": "INBOX", "name": "INBOX", "role": "inbox", "total": None, "unread": None},
        {"id": "&XfJT0ZAB-", "name": "&XfJT0ZAB-", "role": None},
    ]}
    assert parse_folders(data) == [
        Folder("INBOX", "INBOX", "inbox"), Folder("&XfJT0ZAB-", "已发送", None)]
    assert parse_folders({"mailboxes": []}) == []


@pytest.mark.parametrize("bad", [
    None, [], "text", 5, {}, {"envelopes": "x"}, {"envelopes": [5]}, {"envelopes": [{}]},
    {"envelopes": [{"id": ""}]}, {"envelopes": [{"id": "1", "from": 5}]},
    {"envelopes": [{"id": "1", "from": ["x"]}]}, {"envelopes": [{"id": "1", "flags": "x"}]},
])
def test_unexpected_shape_is_a_clear_error(bad):
    with pytest.raises(HimalayaError) as raised:
        parse_envelopes(bad)
    text = str(raised.value)
    assert text.startswith("himalaya printed something mx does not understand: ")
    assert len(text) <= len("himalaya printed something mx does not understand: ") + 200


@pytest.mark.parametrize("bad", [None, {}, {"mailboxes": 1}, {"mailboxes": [{"id": "x"}]},
                                 {"mailboxes": [{"name": 5}]}])
def test_unexpected_folder_shape_is_a_clear_error(bad):
    with pytest.raises(HimalayaError, match="does not understand"):
        parse_folders(bad)


def test_the_excerpt_is_cut_and_has_no_control_characters():
    data = {"envelopes": "x\n\u202e" + "y" * 500}
    with pytest.raises(HimalayaError) as raised:
        parse_envelopes(data)
    text = str(raised.value).split(": ", 1)[1]
    assert len(text) == 200
    assert "\n" not in text and "\u202e" not in text


def test_parse_error():
    assert parse_error('{"error":"imap: boom","sources":["inner","deeper"],"backtrace":null}') \
        == "imap: boom: inner: deeper"
    assert parse_error('{"error":"alone","sources":[],"backtrace":null}') == "alone"
    assert parse_error('{"error":"alone"}') == "alone"
    assert parse_error("\n  \nwhat happened\nsecond line\n") == "what happened"
    assert parse_error('{"something":"else"}') == '{"something":"else"}'
    assert parse_error("") == ""
    assert parse_error('["not","an","object"]') == '["not","an","object"]'


# What the real binary prints without --json, run against a refused loopback port on Windows.
PLAIN_REFUSED = (
    "Error: connect 127.0.0.1:1\n\nCaused by:\n"
    " - 由于目标计算机积极拒绝，无法连接。 (os error 10061)\n\n"
    "Suggestions:\n - Run with --log-level to enable more verbose logs\n")
SUGGESTION = "Suggestions:\n - Run with --log-level to enable more verbose logs\n"


def test_parse_error_reads_the_plain_layout():
    refused = "connect 127.0.0.1:1: 由于目标计算机积极拒绝，无法连接。 (os error 10061)"
    assert parse_error(PLAIN_REFUSED) == refused
    assert classify(PLAIN_REFUSED) is ConnectError
    assert parse_error("Error: Account `nope` not found, it holds: qq\n\n" + SUGGESTION) \
        == "Account `nope` not found, it holds: qq"
    assert parse_error("Error: outer\n\nCaused by:\n - middle\n - inner\n\n" + SUGGESTION) \
        == "outer: middle: inner"
    assert parse_error("Error: outer\n\nCaused by:\n - first line\n   second line\n") \
        == "outer: first line\nsecond line"
    assert parse_error("Error:\n\nCaused by:\n - only the cause\n") == "only the cause"
    assert parse_error("Error:\n") == "Error:"
    assert parse_error("\n\nError: after blank lines\n") == "after blank lines"
    assert parse_error("Errors are fine\n") == "Errors are fine"  # not the `Error:` line


def test_classify():
    assert classify("imap: AUTHENTICATIONFAILED") is AuthError
    assert classify("NO [AUTHENTICATIONFAILED] Invalid credentials") is AuthError
    assert classify("smtp: 535 5.7.8 Username and Password not accepted") is AuthError
    assert classify("oauth: invalid_grant") is AuthError
    assert classify("no password stored for qq") is AuthError
    assert classify("connect x:993: Connection refused (os error 111)") is ConnectError
    assert classify("operation timed out") is ConnectError
    assert classify("failed to lookup address information") is ConnectError
    assert classify("something else") is HimalayaError
    assert classify("") is HimalayaError
    assert issubclass(AuthError, HimalayaError) and issubclass(ConnectError, HimalayaError)
    assert issubclass(HimalayaError, MailError)
    assert all(p == p.lower() for p in AUTH_PATTERNS + CONNECT_PATTERNS)


def test_535_is_a_login_failure_only_as_a_number_of_its_own():
    for text in ("smtp: 535 5.7.8 Username and Password not accepted",
                 "535-5.7.8 Username and Password not accepted", "SMTP error 535.", "code=535",
                 "(535)"):
        assert classify(text) is AuthError, text
    for text in ("message 15350 not found", "uid 5350 is gone", "no message 1535", "x535y",
                 "host mail535.example.com", "5.3.5", "5350"):
        assert classify(text) is HimalayaError, text
    # the other patterns stay substrings
    assert classify("noAUTHENTICATIONFAILEDno") is AuthError
    assert classify("xxconnection refusedxx") is ConnectError


def test_windows_connection_errors_are_connect_errors_whatever_the_language():
    """Seen from the real binary on a Chinese Windows: `connect 127.0.0.1:1`, then the system's
    own wording and `(os error 10061)`."""
    text = "connect 127.0.0.1:1: 由于目标计算机积极拒绝，无法连接。 (os error 10061)"
    assert classify(text) is ConnectError


# --- errors and timeouts ----------------------------------------------------------------------


def test_errors_read_from_stdout_json_and_classified(hub, ctx, fake_himalaya):
    fake_himalaya({
        "qq": {}, "gmail": {}, "feishu": {},
        "fail": {
            "qq": "imap: AUTHENTICATIONFAILED",
            "gmail": "failed to connect: connection refused",
            "feishu": "some other trouble",
        }})
    hima = Himalaya(ctx)
    with pytest.raises(AuthError, match="AUTHENTICATIONFAILED"):
        hima.folders("qq")
    with pytest.raises(ConnectError, match="connection refused"):
        hima.search("gmail", "INBOX", [], 1)
    with pytest.raises(HimalayaError, match="some other trouble") as raised:
        hima.status("feishu", "INBOX")
    assert type(raised.value) is HimalayaError


@pytest.mark.parametrize(("failure", "cls", "message"), [
    (["connect imap.qq.com:993", "Connection refused (os error 111)"], ConnectError,
     "connect imap.qq.com:993: Connection refused (os error 111)"),
    (["imap login", "NO [AUTHENTICATIONFAILED] Invalid credentials"], AuthError,
     "imap login: NO [AUTHENTICATIONFAILED] Invalid credentials"),
    (["something broke", "inner detail"], HimalayaError, "something broke: inner detail"),
    ("plain", HimalayaError, "plain"),
])
def test_a_failure_in_the_plain_layout_is_classified_with_its_cause(
        hub, ctx, fake_himalaya, failure, cls, message):
    """`raw()` runs without --json, so the cause sits on a `Caused by:` line, not in a field."""
    fake = fake_himalaya({"qq": {}, "fail": {"qq": failure}})
    hima = Himalaya(ctx)
    with pytest.raises(cls) as plain:
        hima.raw("qq", "INBOX", "1")
    assert fake.calls()[0]["argv"][5] != "--json"  # the plain layout was the one asked for
    assert type(plain.value) is cls
    assert str(plain.value) == message and "Error:" not in str(plain.value)
    with pytest.raises(cls) as with_json:  # the same failure with --json reads the same
        hima.folders("qq")
    assert str(with_json.value) == message


def test_the_whole_output_classifies_but_the_message_is_the_error(hub, ctx, make_exe):
    make_exe("himalaya", (
        "import json, sys\n"
        "print(json.dumps({'error': 'it failed', 'sources': [], 'backtrace': None}))\n"
        "print('operation timed out', file=sys.stderr)\n"
        "sys.exit(1)\n"))
    with pytest.raises(ConnectError) as raised:
        Himalaya(ctx).folders("qq")
    assert str(raised.value) == "it failed"


def test_a_number_inside_a_message_is_not_a_login_failure(hub, ctx, make_exe):
    make_exe("himalaya", "print('Error: message 15350 not found')\nraise SystemExit(1)\n")
    with pytest.raises(HimalayaError, match="^message 15350 not found$") as raised:
        Himalaya(ctx).raw("qq", "INBOX", "15350")
    assert type(raised.value) is HimalayaError


def test_the_fake_reports_its_own_crash_as_97(hub, ctx, make_exe):
    server, log = hub.state / "server.json", hub.state / "calls.log"
    server.write_text("this is not json", "utf-8")
    make_exe("himalaya", himalaya_body(server, log))
    with pytest.raises(HimalayaError, match="fake himalaya crashed"):
        Himalaya(ctx).folders("qq")
    assert json.loads(log.read_text("utf-8").splitlines()[0])["exit"] == 97


def test_the_error_comes_from_stdout_not_stderr(hub, ctx, make_exe):
    make_exe("himalaya", (
        "import json, sys\n"
        "print(json.dumps({'error': 'boom', 'sources': ['inner'], 'backtrace': None}))\n"
        "print('STDERR NOISE', file=sys.stderr)\n"
        "sys.exit(1)\n"))
    with pytest.raises(HimalayaError) as raised:
        Himalaya(ctx).folders("qq")
    assert str(raised.value) == "boom: inner"


def test_without_stdout_the_error_falls_back_to_stderr_then_the_status(hub, ctx, make_exe):
    make_exe("himalaya", "import sys; print('bad option', file=sys.stderr); sys.exit(2)")
    with pytest.raises(HimalayaError, match="^bad option$"):
        Himalaya(ctx).folders("qq")
    make_exe("himalaya", "import sys; sys.exit(3)")
    with pytest.raises(HimalayaError, match="exited with status 3"):
        Himalaya(ctx).folders("qq")


def test_a_long_error_is_cut_at_300_characters(hub, ctx, make_exe):
    make_exe("himalaya", (
        "import json, sys\n"
        "print(json.dumps({'error': 'x' * 1000, 'sources': [], 'backtrace': None}))\n"
        "sys.exit(1)\n"))
    with pytest.raises(HimalayaError) as raised:
        Himalaya(ctx).folders("qq")
    assert str(raised.value) == "x" * 300


def test_an_error_after_300_characters_still_classifies(hub, ctx, make_exe):
    make_exe("himalaya", (
        "import json, sys\n"
        "print(json.dumps({'error': 'x' * 400 + ' AUTHENTICATIONFAILED', 'sources': []}))\n"
        "sys.exit(1)\n"))
    with pytest.raises(AuthError) as raised:
        Himalaya(ctx).folders("qq")
    assert len(str(raised.value)) == 300


def test_output_that_is_not_json_is_a_clear_error(hub, ctx, make_exe):
    make_exe("himalaya", "print('hello there')")
    with pytest.raises(HimalayaError, match="does not understand: hello there"):
        Himalaya(ctx).folders("qq")


def test_timeout_raises_connect_error(hub, ctx, make_exe):
    make_exe("himalaya", "import time; time.sleep(60)")
    started = time.monotonic()
    with pytest.raises(ConnectError, match=r"did not answer within 1 s"):
        Himalaya(ctx, timeout=1).folders("qq")
    assert time.monotonic() - started < 30  # the child was killed, not waited for


def test_a_timeout_kills_the_children_of_himalaya_too(hub, ctx, make_exe):
    """himalaya runs the password command; one that is still alive must not keep our pipes."""
    make_exe("himalaya", (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "time.sleep(60)\n"))
    started = time.monotonic()
    with pytest.raises(ConnectError):
        Himalaya(ctx, timeout=1).folders("qq")
    assert time.monotonic() - started < 30


def test_the_default_timeout_is_90_seconds(ctx):
    assert Himalaya(ctx).timeout == 90.0


# --- the methods ------------------------------------------------------------------------------


def test_folders(hub, ctx, fake_himalaya):
    server = make_server()
    server["qq"][utf7("已发送")] = {"uidvalidity": "1", "messages": []}
    fake_himalaya(server)
    found = Himalaya(ctx).folders("qq")
    assert [f.name for f in found] == [
        "INBOX", "Sent Messages", "Deleted Messages", "&XfJT0ZAB-"]
    assert [f.shown for f in found][-1] == "已发送"
    assert found[0].role == "inbox" and found[1].role is None


def test_search_returns_parsed_envelopes_newest_first(hub, ctx, fake_himalaya):
    fake_himalaya(make_server())
    found = Himalaya(ctx).search("qq", "INBOX", [], 10)
    assert [e.id for e in found] == ["5", "2", "1"]
    assert found[0] == Envelope(
        "5", "季度发票", "Carol", "carol@example.org",
        datetime(2026, 10, 19, 10, 0, tzinfo=SHANGHAI), False, False)
    assert found[2].seen is True


def test_search_asks_for_the_attachment_column(hub, ctx, fake_himalaya):
    server = make_server()
    server["qq"]["INBOX"]["messages"].append({
        "uid": 6, "date": "Tue, 20 Oct 2026 08:00:00 +0800", "seen": False,
        "raw": eml_with_attachment(
            "receipt", "Shop <shop@example.org>", "Tue, 20 Oct 2026 08:00:00 +0800")})
    fake = fake_himalaya(server)
    hima = Himalaya(ctx)

    found = hima.search("qq", "INBOX", [], 10)
    attachments = {e.id: e.has_attachment for e in found}
    assert attachments == {"6": True, "5": False, "2": False, "1": False}
    # A flag, so it goes before the query words.
    assert words(fake.calls()[0])[:9] == [
        "envelope", "search", "-m", "INBOX", "-s", "10", "-p", "1", "--has-attachment"]

    # The fake, like himalaya, fills the field only when asked: without the flag it is null.
    plain = hima.run("qq", "envelope", "search", "-m", "INBOX", "-s", "10", "-p", "1")
    assert {e.has_attachment for e in parse_envelopes(json.loads(plain))} == {None}


def test_search_understands_the_query_words(hub, ctx, fake_himalaya):
    fake_himalaya(make_server())
    hima = Himalaya(ctx)

    def ids(*query: str, limit: int = 10) -> list[str]:
        return [e.id for e in hima.search("qq", "INBOX", list(query), limit)]

    assert ids("from", '"bob"') == ["1"]
    assert ids("from", '"CAROL@example"') == ["5"]
    assert ids("subject", '"invoice"') == ["1"]
    assert ids("subject", '"发票"') == ["5"]
    assert ids("body", '"noodles"') == ["2"]
    assert ids("date", "2026-10-18") == ["2"]
    assert ids("after", "2026-10-17") == ["5", "2"]
    assert ids("after", "2026-10-18") == ["5"]  # strictly after the day
    assert ids("not", "flag", "seen") == ["5", "2"]
    assert ids("flag", "seen") == ["1"]
    assert ids("not", "after", "2026-10-17") == ["1"]
    assert ids("(", "from", '"bob"', "or", "from", '"alice"', ")", "and", "not", "flag", "seen") \
        == ["2"]
    assert ids("from", '"nobody"', "or", "subject", "lunch") == ["2"]
    assert ids("subject", '"a \\"b\\" c"') == []
    assert ids(limit=2) == ["5", "2"]


def test_search_rejects_what_himalaya_does_not_know(hub, ctx, fake_himalaya):
    fake_himalaya(make_server())
    with pytest.raises(HimalayaError, match="before"):
        Himalaya(ctx).search("qq", "INBOX", ["before", "2026-10-19"], 5)
    with pytest.raises(HimalayaError, match="NONEXISTENT"):
        Himalaya(ctx).search("qq", "No such folder", [], 5)


def test_status(hub, ctx, fake_himalaya):
    server = make_server()
    server["gmail"]["INBOX"]["uidnext"] = 40
    fake_himalaya(server)
    hima = Himalaya(ctx)
    assert hima.status("qq", "INBOX") == ("7", 6)
    assert hima.status("qq", "Sent Messages") == ("7", 1)
    assert hima.status("gmail", "INBOX") == ("3", 40)


def test_status_without_the_uids_is_an_error(hub, ctx, make_exe):
    make_exe("himalaya", "print('{\"messages\": 3, \"uid_next\": null, \"uid_validity\": null}')")
    with pytest.raises(HimalayaError, match="does not understand"):
        Himalaya(ctx).status("qq", "INBOX")


def test_fetch_dates_and_the_highest_uid_quirk(hub, ctx, fake_himalaya):
    server = make_server()
    server["qq"]["INBOX"]["messages"][2]["internal_date"] = "2026-10-19T03:00:00Z"
    fake_himalaya(server)
    hima = Himalaya(ctx)
    got = hima.fetch_dates("qq", "INBOX", "2:*")
    assert sorted(got) == [2, 5]
    assert got[5] == datetime(2026, 10, 19, 3, 0, tzinfo=UTC)
    assert got[2] == datetime(2026, 10, 18, 12, 0, tzinfo=SHANGHAI)
    assert sorted(hima.fetch_dates("qq", "INBOX", "6:*")) == [5]  # nothing newer: still answers 5
    assert sorted(hima.fetch_dates("qq", "INBOX", "1,5")) == [1, 5]
    assert hima.fetch_dates("qq", "Sent Messages", "1:*") == {}


def test_uids_since_uses_the_received_date(hub, ctx, fake_himalaya):
    server = make_server()
    server["qq"]["INBOX"]["messages"][0]["internal_date"] = "2026-10-20T01:00:00+08:00"
    fake_himalaya(server)
    hima = Himalaya(ctx)
    assert hima.uids_since("qq", "INBOX", date(2026, 10, 18)) == [1, 2, 5]
    assert hima.uids_since("qq", "INBOX", date(2026, 10, 19)) == [1, 5]
    assert hima.uids_since("qq", "INBOX", date(2026, 10, 21)) == []


def test_fetch_raw_decodes_base64_and_keeps_bytes(hub, ctx, fake_himalaya):
    gb = (b"From: a@qq.com\r\nSubject: hi\r\nContent-Type: text/plain; charset=gb18030\r\n"
          b"Content-Transfer-Encoding: 8bit\r\n\r\n" + "你好，世界".encode("gb18030") + b"\r\n")
    assert not _is_utf8(gb)
    server = make_server()
    server["qq"]["INBOX"]["messages"].append(
        {"uid": 6, "date": "Mon, 19 Oct 2026 11:00:00 +0800", "seen": False,
         "raw_b64": base64.b64encode(gb).decode()})
    fake_himalaya(server)
    hima = Himalaya(ctx)
    got = hima.fetch_raw("qq", "INBOX", [6, 2, 99])
    assert sorted(got) == [2, 6]  # 99 is not on the server: absent, not an error
    assert got[6] == gb
    assert "你好，世界" in got[6].decode("gb18030")
    assert got[2].startswith(b"From: Alice")
    assert hima.fetch_raw("qq", "INBOX", []) == {}


def _is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def test_raw_gives_the_exact_bytes(hub, ctx, fake_himalaya):
    server = make_server()
    text = eml("季度发票", "Carol <carol@example.org>", "Mon, 19 Oct 2026 10:00:00 +0800")
    text = text.replace("\n", "\r\n")
    server["qq"]["INBOX"]["messages"][2]["raw"] = text
    fake_himalaya(server)
    hima = Himalaya(ctx)
    assert hima.raw("qq", "INBOX", "5") == text.encode("utf-8")
    assert hima.fetch_raw("qq", "INBOX", [5])[5] == text.encode("utf-8")


def test_raw_of_a_missing_mail_is_an_error_read_from_the_plain_output(hub, ctx, fake_himalaya):
    fake_himalaya(make_server())
    with pytest.raises(HimalayaError, match="99"):
        Himalaya(ctx).raw("qq", "INBOX", "99")


def test_raw_of_nothing_is_an_error(hub, ctx, make_exe):
    make_exe("himalaya", "pass")
    with pytest.raises(HimalayaError, match="no message"):
        Himalaya(ctx).raw("qq", "INBOX", "1")


def test_mark_seen_touches_only_that_mail(hub, ctx, fake_himalaya):
    fake = fake_himalaya(make_server())
    Himalaya(ctx).mark_seen("qq", "INBOX", "2")
    flags = {m["uid"]: m["seen"] for m in fake.server()["qq"]["INBOX"]["messages"]}
    assert flags == {1: True, 2: True, 5: False}


def test_move_moves_the_mail(hub, ctx, fake_himalaya):
    fake = fake_himalaya(make_server())
    Himalaya(ctx).move("qq", "INBOX", "2", "Deleted Messages")
    server = fake.server()["qq"]
    assert [m["uid"] for m in server["INBOX"]["messages"]] == [1, 5]
    moved = server["Deleted Messages"]["messages"]
    assert [m["uid"] for m in moved] == [1] and "lunch" in moved[0]["raw"]


def test_move_with_nothing_moved_is_an_error(hub, ctx, fake_himalaya):
    fake = fake_himalaya(make_server())
    with pytest.raises(HimalayaError, match="No message moved") as raised:
        Himalaya(ctx).move("qq", "INBOX", "99", "Deleted Messages")
    assert type(raised.value) is HimalayaError
    assert len(fake.server()["qq"]["INBOX"]["messages"]) == 3


def test_move_to_a_missing_folder_is_an_error(hub, ctx, fake_himalaya):
    fake_himalaya(make_server())
    with pytest.raises(HimalayaError, match="TRYCREATE"):
        Himalaya(ctx).move("qq", "INBOX", "2", "Nowhere")


def test_send_gives_himalaya_the_mail_on_stdin(hub, ctx, fake_himalaya):
    fake = fake_himalaya(make_server())
    mail = b"From: me@qq.com\r\nTo: you@example.org\r\nSubject: hi\r\n\r\nbody\r\n"
    assert Himalaya(ctx).send("qq", mail) is None
    [call] = fake.calls()
    assert call["stdin"].encode() == mail
    sent = fake.server()["_sent"]
    assert [(s["mailbox"], s["raw"].encode()) for s in sent] == [("qq", mail)]


def test_send_refuses_8bit_input(hub, ctx, fake_himalaya):
    fake = fake_himalaya(make_server())
    with pytest.raises(MailError, match="7-bit"):
        Himalaya(ctx).send("qq", "Subject: 你好\r\n\r\n你好\r\n".encode())
    with pytest.raises(MailError, match="7-bit"):
        Himalaya(ctx).add("qq", "Sent Messages", b"Subject: caf\xe9\r\n\r\nx\r\n")
    with pytest.raises(MailError, match="empty"):
        Himalaya(ctx).send("qq", b"")
    assert fake.calls() == []  # nothing ran


def test_the_fake_cuts_at_the_first_8bit_line_like_himalaya(hub, ctx, fake_himalaya):
    """What F-SEND warns about, so the 7-bit check is worth having: via run(), which has none."""
    fake = fake_himalaya(make_server())
    Himalaya(ctx).run("qq", "message", "send", "--no-save", input=b"A: 1\r\nB: \xe4\r\nC: 3\r\n")
    assert fake.server()["_sent"][0]["raw"] == "A: 1\r\n"


def test_a_failing_smtp_backend_fails_the_send(hub, ctx, fake_himalaya):
    fake_himalaya({
        **make_server(), "fail": {"gmail": {"smtp": "535 5.7.8 Authentication failed"}}})
    hima = Himalaya(ctx)
    assert hima.folders("gmail")  # IMAP is fine
    with pytest.raises(AuthError, match="535"):
        hima.send("gmail", b"Subject: x\r\n\r\nhi\r\n")


def test_add_appends_a_seen_copy(hub, ctx, fake_himalaya):
    fake = fake_himalaya(make_server())
    mail = b"From: me@qq.com\r\nTo: you@example.org\r\nSubject: copy\r\n\r\nbody\r\n"
    Himalaya(ctx).add("qq", "Sent Messages", mail)
    [added] = fake.server()["qq"]["Sent Messages"]["messages"]
    assert added["uid"] == 1 and added["seen"] is True
    assert added["raw"].encode() == mail
    Himalaya(ctx).add("qq", "Sent Messages", mail)
    assert [m["uid"] for m in fake.server()["qq"]["Sent Messages"]["messages"]] == [1, 2]
    with pytest.raises(HimalayaError, match="TRYCREATE"):
        Himalaya(ctx).add("qq", "Nowhere", mail)


def test_check_reports_each_backend(hub, ctx, fake_himalaya):
    fake_himalaya({
        **make_server(), "fail": {"gmail": {"smtp": "535 5.7.8 Authentication failed"}}})
    hima = Himalaya(ctx)
    assert hima.check("qq") == [("imap", True, None), ("smtp", True, None)]
    assert hima.check("gmail") == [
        ("imap", True, None), ("smtp", False, "535 5.7.8 Authentication failed")]


# --- folders ----------------------------------------------------------------------------------


def test_decode_utf7():
    assert decode_utf7("&XfJT0ZAB-") == "已发送" and decode_utf7("Sent Messages") == "Sent Messages"
    assert decode_utf7("~peter/mail/&U,BTFw-/&ZeVnLIqe-") == "~peter/mail/台北/日本語"  # RFC 3501
    assert decode_utf7("Q&-A") == "Q&A"
    assert decode_utf7("&2D3eAA-") == "\U0001f600"  # a surrogate pair
    assert decode_utf7("[Gmail]/&XfJT0ZCuTvY-") == "[Gmail]/已发邮件"
    assert decode_utf7("[Gmail]/&XfJSIJZkkK5O9g-") == "[Gmail]/已删除邮件"
    assert decode_utf7("") == ""


@pytest.mark.parametrize("name", [
    "R&D", "&XfJT0ZAB", "&!!!-", "&XfJT0ZA-", "已发送", "tab\there", "&AAAA-xé",
    "&2D0-",
])
def test_a_name_that_is_not_modified_utf7_comes_back_as_it_is(name):
    assert decode_utf7(name) == name


def test_utf7_helper_matches_the_decoder():
    for text in ("已发送", "[Gmail]/已发邮件", "归档", "Sent"):
        assert decode_utf7(utf7(text)) == text


def folder(name: str) -> Folder:
    return Folder(name, decode_utf7(name), None)


def test_guess_folders_qq_gmail_feishu():
    qq = [folder(n) for n in (
        "INBOX", "Drafts", "Sent Messages", "Deleted Messages", "Junk", utf7("已发送"))]
    assert guess_folders(qq) == {
        "sent": "Sent Messages", "trash": "Deleted Messages", "archive": None}
    gmail = [folder(n) for n in (
        "INBOX", "[Gmail]/Drafts", "[Gmail]/Sent Mail", "[Gmail]/Trash", "[Gmail]/All Mail")]
    assert guess_folders(gmail) == {
        "sent": "[Gmail]/Sent Mail", "trash": "[Gmail]/Trash", "archive": "[Gmail]/All Mail"}
    gmail_zh = [folder(n) for n in (
        "INBOX", utf7("[Gmail]/已发邮件"), utf7("[Gmail]/已删除邮件"), "[Gmail]/All Mail")]
    assert guess_folders(gmail_zh) == {
        "sent": utf7("[Gmail]/已发邮件"), "trash": utf7("[Gmail]/已删除邮件"),
        "archive": "[Gmail]/All Mail"}
    feishu = [folder(n) for n in ("INBOX", utf7("已发送"), utf7("已删除"), utf7("归档"), "sent items")]
    assert guess_folders(feishu) == {
        "sent": "sent items", "trash": utf7("已删除"), "archive": utf7("归档")}
    assert guess_folders([folder("INBOX")]) == {"sent": None, "trash": None, "archive": None}
    assert guess_folders([]) == {"sent": None, "trash": None, "archive": None}


def test_guess_folders_prefers_the_earlier_known_name_and_ignores_case():
    folders = [folder("SENT"), folder("sent messages"), folder("Trash")]
    assert guess_folders(folders)["sent"] == "sent messages"
    assert guess_folders(folders)["trash"] == "Trash"
    assert set(himalaya.KNOWN_FOLDERS) == {"sent", "trash", "archive"}


# --- the commands -----------------------------------------------------------------------------


def run_cli(hub, capsys, *argv: str) -> tuple[int, str, str]:
    code = main(["--hub", str(hub.root), *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_folders_command(hub, ctx, fake_himalaya, capsys):
    server = make_server()
    server["qq"][utf7("已发送")] = {"uidvalidity": "1", "messages": []}
    fake_himalaya(server)
    code, out, err = run_cli(hub, capsys, "folders", "qq")
    assert code == 0 and err == ""
    assert json.loads(out) == {
        "folders": [
            {"name": "INBOX", "shown": "INBOX"},
            {"name": "Sent Messages", "shown": "Sent Messages"},
            {"name": "Deleted Messages", "shown": "Deleted Messages"},
            {"name": "&XfJT0ZAB-", "shown": "已发送"}],
        "guess": {"sent": "Sent Messages", "trash": "Deleted Messages", "archive": None}}
    assert "已发送" in out  # not escaped


def test_folders_command_errors(hub, ctx, fake_himalaya, capsys):
    fake_himalaya({"qq": {}, "gmail": {}, "fail": {"qq": "imap: AUTHENTICATIONFAILED"}})
    code, out, err = run_cli(hub, capsys, "folders", "qq")
    assert (code, out) == (1, "") and err == "mx: imap: AUTHENTICATIONFAILED\n"
    code, out, err = run_cli(hub, capsys, "folders", "nobody")
    assert (code, out) == (1, "") and err.startswith("mx: no mailbox nobody")


def test_check_reports_each_mailbox(hub, ctx, fake_himalaya, capsys):
    fake_himalaya({
        **make_server(), "fail": {"gmail": {"smtp": "535 5.7.8 Authentication failed"}}})
    code, out, err = run_cli(hub, capsys, "check")
    assert code == 0 and err == ""
    assert json.loads(out) == {
        "qq": {"ok": True},
        "gmail": {"ok": False, "kind": "auth", "error": "smtp: 535 5.7.8 Authentication failed"}}
    code, out, _ = run_cli(hub, capsys, "check", "qq")
    assert code == 0 and json.loads(out) == {"qq": {"ok": True}}


def test_check_tries_a_search_after_the_backends_pass(hub, ctx, fake_himalaya, capsys):
    server = make_server()
    del server["qq"]["INBOX"]
    fake = fake_himalaya(server)
    code, out, _ = run_cli(hub, capsys, "check", "qq")
    result = json.loads(out)["qq"]
    assert code == 0 and result["ok"] is False and result["kind"] == "other"
    assert "NONEXISTENT" in result["error"]
    assert [words(c)[:2] for c in fake.calls()] == [["account", "check"], ["envelope", "search"]]
    assert words(fake.calls()[1])[:8] == ["envelope", "search", "-m", "INBOX", "-s", "1", "-p", "1"]


def test_check_kinds(hub, ctx, fake_himalaya, capsys):
    fake_himalaya({
        "qq": {}, "gmail": {},
        "fail": {"qq": "AUTHENTICATIONFAILED", "gmail": "connect x:993: connection refused"}})
    code, out, _ = run_cli(hub, capsys, "check")
    assert code == 0
    result = json.loads(out)
    assert result["qq"]["kind"] == "auth" and "AUTHENTICATIONFAILED" in result["qq"]["error"]
    assert result["gmail"]["kind"] == "connect" and "refused" in result["gmail"]["error"]


def test_check_uses_the_inbox_folder_of_the_mailbox(hub, ctx, fake_himalaya, capsys):
    write_mailboxes(hub, BOXES + '\n[mailbox.folders]\ninbox = "Eingang"\n')
    server = make_server()
    server["gmail"] = {"Eingang": server["gmail"]["INBOX"]}
    fake_himalaya(server)
    code, out, _ = run_cli(hub, capsys, "check", "gmail")
    assert code == 0 and json.loads(out) == {"gmail": {"ok": True}}


def test_check_without_himalaya_is_one_clear_error(hub, ctx, monkeypatch, tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    code, out, err = run_cli(hub, capsys, "check")
    assert (code, out) == (1, "") and "mx install-tool himalaya" in err


# --- the real binary, offline -----------------------------------------------------------------

# The keys each parser reads, by the json-schema key of the command it reads.
READS = {
    "himalaya-envelope-search": [
        "envelopes", "id", "subject", "from", "name", "email", "date", "flags", "raw", "iana",
        "has-attachment"],
    "himalaya-mailbox-list": ["mailboxes", "name", "role"],
    "himalaya-imap-status": ["uid_next", "uid_validity"],
    "himalaya-imap-fetch": ["messages", "uid", "internal_date", "body"],
    "himalaya-imap-search": ["uid_mode", "ids", "id"],
    "himalaya-account-check": ["backends", "backend", "ok", "error"],
    "himalaya-message-send": [],  # only the exit status is read
    "himalaya-message-add": [],
    "himalaya-flag-add": [],  # the fake answers it the way the schema says
}


def property_names(schema) -> set[str]:
    """Every property name anywhere in a JSON Schema, `$defs` included."""
    names: set[str] = set()
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                names.update(value)
            names |= property_names(value)
    elif isinstance(schema, list):
        for item in schema:
            names |= property_names(item)
    return names


@pytest.mark.himalaya
def test_json_schema_has_the_fields_we_read():
    for key, reads in READS.items():
        done = subprocess.run(
            [shutil.which("himalaya"), "json-schema", key], check=True, capture_output=True,
            stdin=subprocess.DEVNULL, timeout=60)
        schema = json.loads(done.stdout)
        names = {norm(n) for n in property_names(schema)}
        missing = [name for name in reads if norm(name) not in names]
        assert not missing, f"{key}: no {missing} in the schema"
        captured = json.loads((FIXTURES / "schema" / f"{key}.json").read_text("utf-8"))
        assert schema == captured, f"{key}: himalaya's schema changed: capture it again"


@pytest.mark.himalaya
def test_the_real_binary_failing_to_connect_is_a_connect_error(hub, ctx):
    """A loopback port that nothing listens on: no server is contacted. `raw()` gets the plain
    layout (Error, Caused by, Suggestions), `folders()` the JSON; both say the same."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # free now, and nothing listens on it
    config = (
        "[accounts.qq]", "default = true", "email = 'me@qq.com'",
        f"imap.server = 'imap://127.0.0.1:{port}'",
        "imap.sasl.plain.username = 'me@qq.com'", "imap.sasl.plain.password.raw = 'x'")
    (hub.state / "himalaya.toml").write_text("\n".join(config), "utf-8", newline="")
    hima = Himalaya(ctx, timeout=60)
    with pytest.raises(ConnectError) as plain:
        hima.raw("qq", "INBOX", "1")
    with pytest.raises(ConnectError) as with_json:
        hima.folders("qq")
    assert str(plain.value).startswith(f"connect 127.0.0.1:{port}: ")
    assert "Error:" not in str(plain.value) and "Suggestions" not in str(plain.value)
    assert str(plain.value) == str(with_json.value)


def test_the_captured_schemas_carry_the_fields_we_read():
    """The same check on the files in git, for machines without himalaya."""
    for key, reads in READS.items():
        schema = load_fixture(f"schema/{key}.json")
        names = {norm(n) for n in property_names(schema)}
        assert all(norm(name) in names for name in reads), key


def test_the_sample_envelopes_follow_the_schema():
    names = {norm(n) for n in property_names(load_fixture("schema/himalaya-envelope-search.json"))}
    for sample in ("envelopes-kebab.json", "envelopes-camel.json"):
        for envelope in load_fixture(sample)["envelopes"]:
            assert {norm(key) for key in envelope} <= names, sample

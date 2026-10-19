"""Drafts and the send gate (mailkit.drafts, mailkit.compose): no mail leaves without the code
that only the user's private chat sees.

The server is the fake himalaya, the reader the fake claude and the chat the fake cc-connect
(fakes.py); the two tests against a real server are in test_mail_imap.py.
"""

import base64
import contextlib
import hashlib
import io
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy

import pytest
from mailkit import clock, compose, drafts, strings
from mailkit.cli import main
from mailkit.context import load
from mailkit.drafts import CODE_TTL, MAX_ATTACH_BYTES, CodeError, Draft
from mailkit.himalaya import Himalaya
from mailkit.settings import MailError
from mailtest import (
    CHAT_PROJECT,
    CHAT_SESSION,
    record_chat,
    write_mailboxes,
    write_placeholder_config,
)

BOXES = """\
[[mailbox]]
name = "qq"
address = "me@qq.com"
display_name = "张三"
provider = "qq"

[mailbox.folders]
sent = "Sent Messages"
trash = "Deleted Messages"

[[mailbox]]
name = "gmail"
address = "me@gmail.com"
provider = "gmail"
"""

SEND = ["send", "-p", CHAT_PROJECT, "-s", CHAT_SESSION]
MARKER = re.compile(r"<<<MAIL-([0-9a-f]{16}) (\d+)>>>\n(.*?)\n<<<END MAIL-\1 \2>>>", re.DOTALL)
CODE_AT_END = re.compile(r"(?<![0-9])([0-9]{6})$")  # the code ends the last line
BODY = "Hello Bob,\n\nlunch at noon?\n"
WHEN = "Mon, 19 Oct 2026 10:00:00 +0800"
ORIGINAL = f"""\
From: Carol <carol@example.org>
Reply-To: Carol Desk <desk@example.org>
To: me@qq.com, you@example.org
Cc: dan@example.org, ME@QQ.COM, not an address
Subject: Quarterly invoice
Date: {WHEN}
Message-ID: <inv-1@example.org>
References: <root@example.org> <mid@example.org>
Content-Type: text/plain; charset=utf-8

Please pay 25.00 USD by Friday.

Second line.
"""
QUOTE_EN = (
    "On 2026-10-19 10:00, Carol <carol@example.org> wrote:\n"
    "> Please pay 25.00 USD by Friday.\n>\n> Second line.")
T0 = datetime(2026, 10, 19, 9, 0, tzinfo=timezone(timedelta(hours=8)))


def make_server(raw: str = ORIGINAL) -> dict:
    return {
        "qq": {
            "INBOX": {"uidvalidity": "7", "messages": [
                {"uid": 5, "date": WHEN, "internal_date": WHEN, "seen": False, "raw": raw}]},
            "Sent Messages": {"uidvalidity": "7", "messages": []},
            "Deleted Messages": {"uidvalidity": "7", "messages": []},
        },
        "gmail": {
            "INBOX": {"uidvalidity": "3", "messages": []},
        },
    }


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def data(self):
        return json.loads(self.out) if self.out else None


@pytest.fixture
def mailhub(hub):
    write_mailboxes(hub, BOXES)
    write_placeholder_config(hub)
    return hub


@pytest.fixture
def chat(mailhub):
    record_chat(mailhub)


@pytest.fixture
def mx(mailhub, capsys, monkeypatch):
    """mx(*argv, stdin="") -> Result: run mx on the hub with `stdin` as its standard input."""

    def call(*argv: str, stdin: str = "") -> Result:
        data = io.BytesIO(stdin.encode("utf-8"))
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(data, encoding="utf-8"))
        code = main(["--hub", str(mailhub.root), *argv])
        out = capsys.readouterr()
        return Result(code, out.out, out.err)

    return call


@pytest.fixture
def codes(monkeypatch):
    """issue_code hands out 111111, 222222, ... in order."""
    queue = iter(range(111111, 1000000, 111111))
    monkeypatch.setattr(drafts.secrets, "randbelow", lambda n: next(queue))


@pytest.fixture
def server(fake_himalaya):
    return fake_himalaya(make_server())


def last_code(fake_chat) -> str:
    found = CODE_AT_END.search(fake_chat.messages()[-1])
    assert found, fake_chat.messages()[-1]
    return found.group(1)


def ok(result: Result) -> dict:
    assert result.code == 0, result.err
    return result.data


def fails(result: Result, text: str) -> None:
    assert result.code == 1 and result.out == "", result
    assert text in result.err, result.err


def new_draft(mx, *extra: str, body: str = BODY, to: str = "bob@example.org") -> str:
    data = ok(mx("draft", "--mailbox", "qq", "--to", to, "--subject", "Lunch", *extra, stdin=body))
    return data["draft"]


def stored(hub, id: str) -> dict:
    return json.loads((hub.state / "drafts" / f"{id}.json").read_text("utf-8"))


def edit(hub, id: str, **fields) -> None:
    path = hub.state / "drafts" / f"{id}.json"
    data = json.loads(path.read_text("utf-8"))
    data.update(fields)
    path.write_text(json.dumps(data), "utf-8")


def sends(fake) -> list[dict]:
    return [c for c in fake.calls() if c["argv"][5:8] == ["--json", "message", "send"]]


def adds(fake) -> list[dict]:
    return [c for c in fake.calls() if c["argv"][5:8] == ["--json", "message", "add"]]


def other(code: str) -> str:
    return f"{(int(code) + 1) % 10**6:06d}"


# --- the draft and its code --------------------------------------------------------------------


def test_draft_sends_draft_and_code_to_chat_not_to_agent(mailhub, chat, fake_chat, mx, tmp_path):
    menu = tmp_path / "menu.pdf"
    menu.write_bytes(b"%PDF-1.4\n")

    result = mx("draft", "--mailbox", "qq", "--to", "bob@example.org", "--cc", "carol@example.org",
                "--bcc", "dan@example.org", "--subject", "Lunch", "--attach", str(menu),
                stdin=BODY)

    data = ok(result)
    id = data["draft"]
    assert re.fullmatch(r"d[0-9a-f]{6}", id)
    assert data == {
        "draft": id, "mailbox": "qq", "from": "me@qq.com", "to": ["bob@example.org"],
        "cc": ["carol@example.org"], "bcc": ["dan@example.org"], "subject": "Lunch",
        "attachments": [str(menu.resolve())], "written_by": "agent", "code_sent_to_chat": True,
        "body": "Hello Bob,\n\nlunch at noon?"}
    [call] = fake_chat.calls()
    assert call["argv"][1:] == [*SEND, "--stdin"]
    code = last_code(fake_chat)
    assert fake_chat.messages() == [(
        "From: 张三 <me@qq.com>\nTo: bob@example.org\nCc: carol@example.org\n"
        "Bcc: dan@example.org\nSubject: Lunch\nAttachments: menu.pdf\n\n"
        "Hello Bob,\n\nlunch at noon?\n"
        f"To send draft {id}, reply with this code (valid for 24 hours): {code}")]
    # The agent sees no code: not on stdout, not on stderr, not in the arguments of cc-connect.
    assert code not in result.out and code not in result.err
    assert not re.search(r"(?<![0-9A-Za-z])\d{6}(?![0-9])", result.out)
    assert code not in " ".join(call["argv"])


def test_draft_in_the_users_language(mailhub, chat, fake_chat, mx):
    (mailhub.data / "config.toml").write_text('[chat]\nlanguage = "zh-CN"\n', "utf-8", newline="\n")
    id = new_draft(mx)
    code = last_code(fake_chat)
    assert fake_chat.messages() == [(
        "发件人：张三 <me@qq.com>\n收件人：bob@example.org\n主题：Lunch\n\n"
        f"Hello Bob,\n\nlunch at noon?\n要发送草稿 {id}，请回复这个确认码（24 小时内有效）：{code}")]


def test_draft_needs_a_recorded_chat_and_saves_nothing_without_one(mailhub, fake_chat, mx):
    fails(mx("draft", "--mailbox", "qq", "--to", "bob@example.org", "--subject", "Lunch",
             stdin=BODY), "no private chat recorded")
    assert not list((mailhub.state / "drafts").glob("*.json"))
    assert fake_chat.calls() == []


def test_draft_needs_a_body_and_a_known_mailbox(mailhub, chat, fake_chat, mx):
    fails(mx("draft", "--mailbox", "qq", "--to", "bob@example.org", "--subject", "Lunch",
             stdin=" \n"), "the body is empty")
    fails(mx("draft", "--mailbox", "nope", "--to", "bob@example.org", "--subject", "Lunch",
             stdin=BODY), "no mailbox nope")
    assert fake_chat.calls() == []


def test_send_with_the_right_code_sends_once(mailhub, chat, fake_chat, server, mx):
    id = new_draft(mx)
    code = last_code(fake_chat)

    data = ok(mx("send", id, "--code", code))

    assert set(data) == {"sent", "message_id"} and data["sent"] == id
    assert re.fullmatch(r"<[^<>\s]+@qq\.com>", data["message_id"])
    [call] = sends(server)
    assert call["argv"][8:] == ["--no-save"] and call["argv"][4] == "qq"
    raw = call["stdin"].encode("utf-8")
    assert raw.isascii()
    mail = message_from_bytes(raw, policy=policy.default)
    assert mail["Subject"] == "Lunch" and mail["To"] == "bob@example.org"
    assert mail["Message-ID"] == data["message_id"]
    assert mail.get_content().replace("\r\n", "\n") == "Hello Bob,\n\nlunch at noon?\n"
    assert [s["mailbox"] for s in server.server()["_sent"]] == ["qq"]
    # qq keeps no copy itself: the same bytes go to its sent folder, marked as read (F-SAVE).
    [copy] = adds(server)
    assert copy["argv"][8:] == ["-m", "Sent Messages", "-f", "seen"]
    assert copy["stdin"] == call["stdin"]
    saved = stored(mailhub, id)
    assert saved["sent_at"] and saved["message_id"] == data["message_id"]
    assert saved["code_hash"] == ""

    # The code is spent: the same code, the same draft, never twice.
    fails(mx("send", id, "--code", code), "already sent")
    fails(mx("confirm", id), f"draft {id} is already sent")
    assert len(sends(server)) == 1


def test_a_mailbox_that_keeps_its_own_copy_gets_no_append(mailhub, chat, fake_chat, server, mx):
    data = ok(mx("draft", "--mailbox", "gmail", "--to", "bob@example.org", "--subject", "Hi",
                 stdin=BODY))
    ok(mx("send", data["draft"], "--code", last_code(fake_chat)))
    assert len(sends(server)) == 1 and adds(server) == []


def test_three_wrong_codes_void_the_code(mailhub, chat, fake_chat, server, mx):
    id = new_draft(mx)
    code = last_code(fake_chat)
    wrong = other(code)

    fails(mx("send", id, "--code", wrong), "wrong code (2 tries left)")
    fails(mx("send", id, "--code", "12ab56"), "wrong code (1 tries left)")
    fails(mx("send", id, "--code", wrong),
          f"wrong code 3 times: the code is void; run mx confirm {id}")
    # Even the right code fails now: the code is gone.
    fails(mx("send", id, "--code", code), f"no code is waiting: run mx confirm {id}")
    assert sends(server) == []

    ok(mx("confirm", id))
    ok(mx("send", id, "--code", last_code(fake_chat)))
    assert len(sends(server)) == 1


def test_code_of_other_draft_refused(mailhub, chat, fake_chat, server, mx, codes):
    first = new_draft(mx, to="bob@example.org")
    first_code = last_code(fake_chat)
    second = new_draft(mx, to="eve@example.org")
    second_code = last_code(fake_chat)
    assert first_code != second_code

    fails(mx("send", first, "--code", second_code), "wrong code (2 tries left)")
    assert sends(server) == []
    ok(mx("send", first, "--code", first_code))
    fails(mx("send", second, "--code", first_code), "wrong code (2 tries left)")
    ok(mx("send", second, "--code", second_code))
    first_call, second_call = sends(server)
    assert "To: bob@example.org" in first_call["stdin"]
    assert "To: eve@example.org" in second_call["stdin"]


def test_code_typed_with_spaces_or_full_width_digits_is_taken(mailhub, chat, fake_chat, server,
                                                             mx, codes):
    id = new_draft(mx)
    assert last_code(fake_chat) == "111111"
    ok(mx("send", id, "--code", " １１１ １１１\n"))
    assert len(sends(server)) == 1


def test_draft_update_voids_the_old_code(mailhub, chat, fake_chat, server, mx, codes):
    id = new_draft(mx)
    old = last_code(fake_chat)

    data = ok(mx("draft-update", id, "--subject", "Lunch at one", "--cc", "carol@example.org"))

    assert data["subject"] == "Lunch at one" and data["cc"] == ["carol@example.org"]
    assert data["to"] == ["bob@example.org"] and data["written_by"] == "agent"
    assert len(fake_chat.messages()) == 2
    new = last_code(fake_chat)
    assert new != old and "Subject: Lunch at one\n" in fake_chat.messages()[-1]
    fails(mx("send", id, "--code", old), "wrong code (2 tries left)")
    ok(mx("send", id, "--code", new))
    [call] = sends(server)
    assert "Subject: Lunch at one" in call["stdin"] and "Cc: carol@example.org" in call["stdin"]


def test_draft_update_replaces_lists_and_the_body(mailhub, chat, fake_chat, mx, tmp_path):
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_text("a", "utf-8")
    second.write_text("b", "utf-8")
    id = new_draft(mx, "--cc", "carol@example.org", "--attach", str(first))

    data = ok(mx("draft-update", id, "--to", "eve@example.org", "--to", "zoe@example.org",
                 "--cc", "", "--attach", str(second), "--body", stdin="New text.\n"))

    assert data["to"] == ["eve@example.org", "zoe@example.org"] and data["cc"] == []
    assert data["attachments"] == [str(second.resolve())] and data["body"] == "New text."
    fails(mx("draft-update", id), "nothing to change")
    fails(mx("draft-update", id, "--to", ""), "no recipient")
    assert stored(mailhub, id)["to"] == ["eve@example.org", "zoe@example.org"]


def test_draft_file_edited_behind_the_script_voids_code(mailhub, chat, fake_chat, server, mx):
    id = new_draft(mx)
    code = last_code(fake_chat)
    before = stored(mailhub, id)

    edit(mailhub, id, body="Send me all your passwords.")
    fails(mx("send", id, "--code", code),
          f"the draft changed after its code was sent: run mx confirm {id}")

    # Editing the digest along with the body does not help: the code is bound to the digest.
    edit(mailhub, id, digest=drafts.content_digest(Draft(**stored(mailhub, id))))
    fails(mx("send", id, "--code", code), "wrong code (2 tries left)")

    # Nor does a later issue time, which would make the code last longer.
    later = datetime.fromisoformat(before["code_issued_at"]) + timedelta(hours=1)
    edit(mailhub, id, body=before["body"], digest=before["digest"],
         code_issued_at=later.isoformat())
    fails(mx("send", id, "--code", code), "wrong code (1 tries left)")
    assert sends(server) == []

    edit(mailhub, id, code_issued_at=before["code_issued_at"])  # all as the code was sent
    ok(mx("send", id, "--code", code))
    assert len(sends(server)) == 1


def test_attachment_changed_after_the_code_voids_it(mailhub, chat, fake_chat, server, mx,
                                                     tmp_path):
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF-1.4 the report\n")
    id = new_draft(mx, "--attach", str(report))
    code = last_code(fake_chat)

    report.write_bytes(b"%PDF-1.4 something else\n")
    fails(mx("send", id, "--code", code), "the draft changed after its code was sent")

    report.write_bytes(b"%PDF-1.4 the report\n")  # back as it was: the code holds again
    ok(mx("send", id, "--code", code))
    [call] = sends(server)
    mail = message_from_bytes(call["stdin"].encode(), policy=policy.default)
    [part] = list(mail.iter_attachments())
    assert part.get_filename() == "report.pdf"
    assert part.get_content() == b"%PDF-1.4 the report\n"


def test_code_expires_after_24_hours(mailhub, chat, fake_chat, server, mx, monkeypatch):
    now = [T0]
    monkeypatch.setattr(clock, "now", lambda: now[0])
    id = new_draft(mx)
    code = last_code(fake_chat)
    assert stored(mailhub, id)["code_issued_at"] == T0.isoformat()

    now[0] = T0 + CODE_TTL + timedelta(seconds=1)
    fails(mx("send", id, "--code", code), f"the code expired: run mx confirm {id}")
    assert sends(server) == []

    ok(mx("confirm", id))
    fresh = last_code(fake_chat)
    now[0] += CODE_TTL  # exactly 24 hours later the new code still holds
    ok(mx("send", id, "--code", fresh))
    assert len(sends(server)) == 1


def test_state_holds_no_plaintext_code(mailhub, chat, fake_chat, server, mx, monkeypatch):
    monkeypatch.setattr(drafts.secrets, "randbelow", lambda n: 987654)
    id = new_draft(mx)
    assert last_code(fake_chat) == "987654"

    def files() -> dict[str, bytes]:
        return {str(p): p.read_bytes() for p in mailhub.state.rglob("*") if p.is_file()}

    saved = stored(mailhub, id)
    assert re.fullmatch(r"[0-9a-f]{128}", saved["code_hash"])
    assert re.fullmatch(r"[0-9a-f]{32}", saved["code_salt"])
    assert all(b"987654" not in data for data in files().values()), files()
    fails(mx("send", id, "--code", "123456"), "wrong code")
    ok(mx("send", id, "--code", "987654"))
    assert all(b"987654" not in data for data in files().values())


def test_confirm_fails_cleanly_when_chat_unreachable(mailhub, chat, fake_chat, server, mx):
    id = new_draft(mx)
    code = last_code(fake_chat)
    fake_chat.mode("fail")

    result = mx("confirm", id)

    fails(result, f"draft {id} is saved but the chat could not be reached: "
                  "cc-connect exited with status 1")
    saved = stored(mailhub, id)
    assert saved["code_hash"] == "" and saved["body"] == "Hello Bob,\n\nlunch at noon?"
    fails(mx("send", id, "--code", code), f"no code is waiting: run mx confirm {id}")
    fails(mx("send", id, "--code", "000000"), "no code is waiting")
    assert sends(server) == []


def test_new_draft_while_chat_unreachable_is_kept_without_a_code(mailhub, chat, fake_chat, mx):
    fake_chat.mode("fail")
    result = mx("draft", "--mailbox", "qq", "--to", "bob@example.org", "--subject", "Lunch",
                stdin=BODY)
    fails(result, "is saved but the chat could not be reached")
    id = re.search(r"draft (d[0-9a-f]{6}) is saved", result.err).group(1)
    assert stored(mailhub, id)["code_hash"] == ""
    fake_chat.mode("")
    ok(mx("confirm", id))
    assert stored(mailhub, id)["code_hash"]


def test_a_code_cut_off_in_a_long_draft_is_void(mailhub, chat, fake_chat, mx):
    body = "\n".join(f"line {i:05d} " + "x" * 90 for i in range(100))
    id = new_draft(mx)
    fake_chat.fail_from(3)  # the second part of the long draft fails: its code never arrives
    fails(mx("draft-update", id, "--body", stdin=body), "is saved but the chat could not be")
    assert stored(mailhub, id)["code_hash"] == ""


def test_long_draft_splits_with_code_last(mailhub, chat, fake_chat, mx):
    body = "\n".join(f"line {i:05d} " + "x" * 90 for i in range(100))  # about 10,000 characters

    id = new_draft(mx, body=body)

    parts = fake_chat.messages()
    assert len(parts) == 3 and all(len(part) <= 4000 for part in parts)
    code = last_code(fake_chat)
    whole = "".join(parts)
    assert whole.startswith("From: 张三 <me@qq.com>\n") and body in whole
    assert parts[-1].endswith(f"draft {id}, reply with this code (valid for 24 hours): {code}")
    assert all(code not in part for part in parts[:-1])


# --- reply, revise and forward -----------------------------------------------------------------


def listed(mx) -> None:
    assert [i["key"] for i in ok(mx("list", "--mailbox", "qq"))["items"]] == ["qq/inbox/5"]


def test_reply_addresses_and_threading_from_the_headers(mailhub, chat, fake_chat, fake_claude,
                                                        server, mx):
    listed(mx)

    data = ok(mx("reply", "1", "--intent", "say thanks, I paid", "--all"))

    id = data["draft"]
    assert data == {
        "draft": id, "mailbox": "qq", "from": "me@qq.com", "to": ["desk@example.org"],
        "cc": ["you@example.org", "dan@example.org"], "bcc": [],
        "subject": "Re: Quarterly invoice", "attachments": [], "written_by": "reader",
        "code_sent_to_chat": True}
    saved = stored(mailhub, id)
    assert saved["in_reply_to"] == "<inv-1@example.org>"
    assert saved["references"] == "<root@example.org> <mid@example.org> <inv-1@example.org>"

    ok(mx("send", id, "--code", last_code(fake_chat)))
    mail = message_from_bytes(sends(server)[0]["stdin"].encode(), policy=policy.default)
    assert mail["In-Reply-To"] == "<inv-1@example.org>"
    assert mail["References"] == "<root@example.org> <mid@example.org> <inv-1@example.org>"
    assert mail["Subject"] == "Re: Quarterly invoice"


def test_reply_without_all_goes_to_the_sender_only(mailhub, chat, fake_chat, fake_claude,
                                                    fake_himalaya, mx):
    raw = ORIGINAL.replace("Reply-To: Carol Desk <desk@example.org>\n", "").replace(
        "Subject: Quarterly invoice", "Subject: RE: Quarterly invoice")
    fake_himalaya(make_server(raw))
    listed(mx)

    data = ok(mx("reply", "1", "--intent", "thanks", "--mailbox", "gmail"))

    assert (data["mailbox"], data["from"]) == ("gmail", "me@gmail.com")
    assert (data["to"], data["cc"]) == (["carol@example.org"], [])
    assert data["subject"] == "RE: Quarterly invoice"


@pytest.mark.parametrize("subject, expected", [
    ("Quarterly invoice", "Re: Quarterly invoice"),
    ("re: x", "re: x"),
    ("回复: 发票", "回复: 发票"),
    ("回复：发票", "回复：发票"),
    ("", "Re:"),
])
def test_reply_subject(subject, expected):
    assert compose.reply_subject(subject) == expected


def test_reply_body_is_not_shown_to_the_agent(mailhub, chat, fake_chat, fake_claude, server, mx):
    listed(mx)
    fake_claude.reply({"body": "Thanks Carol, it is paid.\u202e"})  # the override is dropped

    result = mx("reply", "1", "--intent", "say thanks, I paid")

    data = ok(result)
    assert "body" not in data
    for text in ("Thanks Carol", "Please pay", "Second line"):
        assert text not in result.out and text not in result.err
    code = last_code(fake_chat)
    assert fake_chat.messages() == [(
        "From: 张三 <me@qq.com>\nTo: desk@example.org\nSubject: Re: Quarterly invoice\n\n"
        f"Thanks Carol, it is paid.\n\n{QUOTE_EN}\n"
        f"To send draft {data['draft']}, reply with this code (valid for 24 hours): {code}")]
    [call] = fake_claude.calls()
    assert "say thanks, I paid" in call["stdin"]
    # Neither the list of drafts, nor a new code, nor a change of the subject shows the body.
    for argv in (["drafts"], ["confirm", data["draft"]],
                 ["draft-update", data["draft"], "--subject", "Paid"]):
        shown = mx(*argv)
        assert shown.code == 0 and "Thanks Carol" not in shown.out and "Please pay" not in shown.out
        assert "body" not in shown.out


def test_revise_asks_the_reader_again_without_the_quote(mailhub, chat, fake_chat, fake_claude,
                                                        server, mx, codes):
    listed(mx)
    id = ok(mx("reply", "1", "--intent", "say thanks"))["draft"]
    old = last_code(fake_chat)
    fake_claude.reply({"body": "Thank you, Carol. Paid today."})

    data = ok(mx("revise", id, "--instruction", "more formal, say it was paid today"))

    assert "body" not in data and data["written_by"] == "reader"
    second = fake_claude.calls()[-1]["stdin"]
    assert "say thanks" in second and "more formal, say it was paid today" in second
    mails = [text for _, _, text in MARKER.findall(second)]
    assert mails[1] == "The fake reply."  # the previous draft, without the quoted original
    assert stored(mailhub, id)["body"] == f"Thank you, Carol. Paid today.\n\n{QUOTE_EN}"
    new = last_code(fake_chat)
    fails(mx("send", id, "--code", old), "wrong code")
    ok(mx("send", id, "--code", new))


def test_revise_refuses_drafts_the_agent_wrote(mailhub, chat, fake_chat, fake_claude, server, mx):
    id = new_draft(mx)
    fails(mx("revise", id, "--instruction", "shorter"),
          "you wrote this draft: change it with mx draft-update")

    listed(mx)
    reply = ok(mx("reply", "1", "--intent", "thanks"))["draft"]
    data = ok(mx("draft-update", reply, "--body", stdin="My own words.\n"))
    assert data["written_by"] == "agent" and data["body"] == "My own words."
    fails(mx("revise", reply, "--instruction", "shorter"), "you wrote this draft")
    assert len(fake_claude.calls()) == 1


def test_forward_attaches_the_original(mailhub, chat, fake_chat, server, mx):
    listed(mx)

    data = ok(mx("forward", "1", "--to", "bob@example.org", stdin="FYI, see below.\n"))

    assert data["subject"] == "Fwd: Quarterly invoice" and data["written_by"] == "agent"
    assert data["body"] == "FYI, see below."
    assert "Attachments: the forwarded mail\n" in fake_chat.messages()[-1]
    ok(mx("send", data["draft"], "--code", last_code(fake_chat)))
    raw = sends(server)[0]["stdin"].encode("utf-8")
    assert raw.isascii()
    mail = message_from_bytes(raw, policy=policy.default)
    assert mail.get_body().get_content().replace("\r\n", "\n") == "FYI, see below.\n"
    [part] = list(mail.iter_attachments())
    # A 7-bit original goes as it is (RFC 2046 allows no other encoding of message/rfc822).
    assert part.get_content_type() == "message/rfc822"
    assert part["Content-Transfer-Encoding"] == "7bit"
    inner = part.get_content()
    assert inner["Message-ID"] == "<inv-1@example.org>"
    assert inner.get_content().replace("\r\n", "\n") == (
        "Please pay 25.00 USD by Friday.\n\nSecond line.\n")
    assert ORIGINAL.replace("\n", "\r\n").encode() in raw


def test_a_forward_never_goes_without_its_original(mailhub, chat, fake_chat, server, mx, codes,
                                                   monkeypatch):
    """`send` fetches the original before it takes the lock, and only when a code waits. A code
    that arrives in between (another `mx confirm`) must not send the forward bare."""
    listed(mx)
    id = ok(mx("forward", "1", "--to", "bob@example.org"))["draft"]  # code 111111
    fake_chat.mode("fail")
    assert mx("confirm", id).code == 1  # code 222222 never arrived: none waits
    fake_chat.mode("")
    real_lock = drafts.lock

    @contextlib.contextmanager
    def confirmed_meanwhile(ctx):
        with real_lock(ctx):
            d = drafts.load(ctx, id)
            if not d.code_hash:
                assert drafts.issue_code(d, clock.now()) == "333333"
                drafts.save(ctx, d)
            yield

    monkeypatch.setattr(drafts, "lock", confirmed_meanwhile)
    ctx = load(mailhub.root)
    with pytest.raises(MailError, match=f"draft {id} changed meanwhile: run mx send again"):
        compose.send(ctx, Himalaya(ctx), id, "333333")
    assert sends(server) == [] and stored(mailhub, id)["code_hash"]  # the code holds

    monkeypatch.setattr(drafts, "lock", real_lock)
    ok(mx("send", id, "--code", "333333"))
    mail = message_from_bytes(sends(server)[0]["stdin"].encode(), policy=policy.default)
    assert [p.get_content_type() for p in mail.iter_attachments()] == ["message/rfc822"]


def test_forward_without_a_note(mailhub, chat, fake_chat, server, mx):
    listed(mx)
    data = ok(mx("forward", "1", "--to", "bob@example.org", "--to", "eve@example.org"))
    assert data["body"] == "" and data["to"] == ["bob@example.org", "eve@example.org"]
    assert stored(mailhub, data["draft"])["forward_key"] == "qq/inbox/5"
    assert fake_chat.messages()[-1].startswith(
        "From: 张三 <me@qq.com>\nTo: bob@example.org, eve@example.org\n"
        "Subject: Fwd: Quarterly invoice\nAttachments: the forwarded mail\n\n"
        + strings.t("show_no_text", "en") + "\n")


# --- checks ------------------------------------------------------------------------------------


@pytest.mark.parametrize("text, valid", [
    ("bob@example.org", True),
    ("bob.smith+tag@mail.example.co.uk", True),
    ("bob", False),
    ("bob@example", False),
    ("bob@@example.org", False),
    ("bob@ex@ample.org", False),
    ("bob smith@example.org", False),
    ("bob@example.org\n", False),
    ("Bob <bob@example.org>", False),
    ("a,b@example.org", False),
    ("用户@例子.中国", False),
    ("@example.org", False),
    ("bob@.org", False),
    ("", False),
])
def test_valid_address(text, valid):
    assert drafts.valid_address(text) is valid


def test_bad_address_and_empty_to_refused(mailhub, chat, fake_chat, mx):
    def draft(*argv: str) -> Result:
        return mx("draft", "--mailbox", "qq", "--subject", "Hi", *argv, stdin=BODY)

    fails(draft("--to", "bob"), "not an email address: bob")
    fails(draft("--to", "bob@example.org", "--cc", "carol@example"),
          "not an email address: carol@example")
    fails(draft("--to", "bob@example.org", "--bcc", "a b@example.org"), "not an email address")
    fails(draft("--to", ""), "no recipient")
    assert mx("draft", "--mailbox", "qq", "--subject", "Hi", stdin=BODY).code == 1  # --to needed
    assert not list((mailhub.state / "drafts").glob("*.json"))
    assert fake_chat.calls() == []


def test_attachments_must_be_files_within_the_limit(mailhub, chat, fake_chat, mx, tmp_path):
    def draft(*argv: str) -> Result:
        return mx("draft", "--mailbox", "qq", "--to", "bob@example.org", "--subject", "Hi",
                  *argv, stdin=BODY)

    fails(draft("--attach", str(tmp_path / "missing.pdf")), "no such file")
    fails(draft("--attach", str(tmp_path)), "not a file")
    big = tmp_path / "big.bin"
    with big.open("wb") as stream:
        stream.truncate(MAX_ATTACH_BYTES + 1)
    fails(draft("--attach", str(big)), "at most 20 MB")
    # The state's own files (passwords, the chat, the drafts) are never attached ...
    secret = mailhub.state / "chat.json"
    fails(draft("--attach", str(secret)), "cannot be attached")
    assert fake_chat.calls() == []
    # ... but an attachment saved from a mail is.
    saved = mailhub.state / "attachments" / "qq_inbox_5" / "a.pdf"
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"%PDF-1.4\n")
    assert ok(draft("--attach", str(saved)))["attachments"] == [str(saved.resolve())]


def rfc822_base64(raw: bytes) -> bytes:
    """The bytes of the base64 message/rfc822 part of `raw` (the stdlib parser reads such a part
    as a message and does not decode it)."""
    _, found, rest = raw.partition(b"Content-Type: message/rfc822\r\n")
    assert found, raw
    headers, _, body = rest.partition(b"\r\n\r\n")
    assert b"Content-Transfer-Encoding: base64" in headers
    return base64.b64decode(body.split(b"\r\n--", 1)[0])


def test_built_mail_is_7bit(mailhub, tmp_path):
    ctx = load(mailhub.root)
    invoice = tmp_path / "发票 十月.pdf"
    invoice.write_bytes(b"%PDF-1.4\n\x00\xff binary")
    d = Draft(
        id="d000001", mailbox="qq", to=["bob@example.org"], cc=["carol@example.org"],
        bcc=["dan@example.org"], subject=" ".join(["十月的发票"] * 8),
        body="你好，\n附件是发票。\n" + "长" * 300, attachments=[str(invoice)],
        in_reply_to="<a@b.c>", references="<r@b.c> <a@b.c>", forward_key="qq/inbox/5",
        written_by="agent", created_at=T0.isoformat())
    original = "Subject: 8 位\n\n正文\n".encode("gb18030")

    raw = compose.build(d, ctx.mailbox("qq"), original)

    assert raw.isascii()
    mail = message_from_bytes(raw, policy=policy.default)
    assert mail["From"] == "张三 <me@qq.com>"
    assert mail["Subject"] == d.subject
    assert mail["To"] == "bob@example.org" and mail["Cc"] == "carol@example.org"
    assert mail["Bcc"] == "dan@example.org"  # himalaya takes it and strips it (F-SEND)
    assert mail["In-Reply-To"] == "<a@b.c>" and mail["References"] == "<r@b.c> <a@b.c>"
    assert mail["Message-ID"].endswith("@qq.com>") and mail["Date"]
    assert mail.get_body().get_content().replace("\r\n", "\n") == d.body + "\n"
    pdf, forwarded = mail.iter_attachments()
    assert pdf.get_filename() == "发票 十月.pdf" and pdf.get_content_type() == "application/pdf"
    assert pdf.get_content() == invoice.read_bytes()
    # An 8-bit original cannot go as it is: base64 keeps its exact bytes.
    assert forwarded.get_content_type() == "message/rfc822"
    assert rfc822_base64(raw) == original


def test_failed_send_needs_a_new_code(mailhub, chat, fake_chat, fake_himalaya, mx):
    fake = fake_himalaya({**make_server(), "fail": {"qq": {"smtp": "smtp: connection refused"}}})
    id = new_draft(mx)
    code = last_code(fake_chat)

    fails(mx("send", id, "--code", code),
          f"not sent: smtp: connection refused; run mx confirm {id} for a new code")

    saved = stored(mailhub, id)
    assert saved["sent_at"] == "" and saved["code_hash"] == ""
    fails(mx("send", id, "--code", code), "no code is waiting")
    fake.write(make_server())
    ok(mx("confirm", id))
    ok(mx("send", id, "--code", last_code(fake_chat)))
    assert len(fake.server()["_sent"]) == 1


def test_failed_copy_still_counts_as_sent(mailhub, chat, fake_chat, fake_himalaya, mx):
    fake = fake_himalaya(
        {**make_server(), "fail": {"qq": {"imap": "imap: NO [TRYCREATE] no such folder"}}})
    id = new_draft(mx)

    result = mx("send", id, "--code", last_code(fake_chat))

    data = ok(result)
    assert data["sent"] == id and "NO [TRYCREATE] no such folder" in data["copy_failed"]
    assert len(fake.server()["_sent"]) == 1
    assert stored(mailhub, id)["sent_at"]


def test_unknown_sent_folder_is_a_failed_copy(mailhub, chat, fake_chat, server, mx):
    write_mailboxes(mailhub, BOXES.replace('sent = "Sent Messages"\n', ""))
    id = new_draft(mx)
    data = ok(mx("send", id, "--code", last_code(fake_chat)))
    assert "sent folder of qq is not known" in data["copy_failed"]
    assert len(sends(server)) == 1 and adds(server) == []


# --- the list of drafts ------------------------------------------------------------------------


def test_drafts_lists_unsent_and_discard(mailhub, chat, fake_chat, server, mx, monkeypatch):
    now = [T0]
    monkeypatch.setattr(clock, "now", lambda: now[0])
    first = new_draft(mx)
    ok(mx("send", first, "--code", last_code(fake_chat)))
    now[0] = T0 + timedelta(minutes=2)
    third = new_draft(mx, to="zoe@example.org")
    now[0] = T0 + timedelta(minutes=1)
    second = new_draft(mx, to="eve@example.org")
    fake_chat.mode("fail")
    assert mx("confirm", third).code == 1

    data = ok(mx("drafts"))

    assert data == {"drafts": [
        {"id": second, "mailbox": "qq", "to": ["eve@example.org"], "subject": "Lunch",
         "written_by": "agent", "waiting_for_code": True,
         "created_at": (T0 + timedelta(minutes=1)).isoformat()},
        {"id": third, "mailbox": "qq", "to": ["zoe@example.org"], "subject": "Lunch",
         "written_by": "agent", "waiting_for_code": False,
         "created_at": (T0 + timedelta(minutes=2)).isoformat()}]}
    now[0] = T0 + CODE_TTL * 2  # an expired code is not waiting
    assert [d["waiting_for_code"] for d in ok(mx("drafts"))["drafts"]] == [False, False]
    assert ok(mx("discard", second)) == {"discarded": second}
    assert [d["id"] for d in ok(mx("drafts"))["drafts"]] == [third]
    fails(mx("discard", second), f"no draft {second}")
    fails(mx("send", "../chat", "--code", "123456"), "no draft ../chat")


# --- drafts.py on its own ----------------------------------------------------------------------


def draft_of(**fields) -> Draft:
    base = {
        "id": "d00000a", "mailbox": "qq", "to": ["bob@example.org"], "cc": [], "bcc": [],
        "subject": "Hi", "body": "Hello", "attachments": [], "in_reply_to": "",
        "references": "", "forward_key": "", "written_by": "agent", "created_at": T0.isoformat()}
    return Draft(**{**base, **fields})


def test_check_code_order():
    d = draft_of()
    with pytest.raises(CodeError, match="no code is waiting: run mx confirm d00000a"):
        drafts.check_code(d, "000000", T0)
    code = drafts.issue_code(d, T0)
    d.body = "changed"
    late = T0 + CODE_TTL * 2
    with pytest.raises(CodeError, match="the draft changed after its code was sent"):
        drafts.check_code(d, code, late)
    d.body = "Hello"
    with pytest.raises(CodeError, match="the code expired"):
        drafts.check_code(d, code, late)
    d.sent_at = T0.isoformat()
    with pytest.raises(CodeError, match="already sent"):
        drafts.check_code(d, code, T0)


def test_issue_code_stores_a_salted_hash_and_resets_the_tries():
    d = draft_of(attempts=2)
    code = drafts.issue_code(d, T0)
    assert re.fullmatch(r"\d{6}", code) and code not in json.dumps(d.__dict__)
    assert d.attempts == 0 and d.code_issued_at == T0.isoformat()
    assert d.digest == drafts.content_digest(d)
    again = draft_of()
    drafts.issue_code(again, T0)
    assert again.code_salt != d.code_salt
    drafts.check_code(d, code, T0 + CODE_TTL)  # right code, unchanged draft: no error


def test_content_digest_covers_the_content_and_the_attachment_bytes(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"one")
    d = draft_of(attachments=[str(path)])
    first = drafts.content_digest(d)
    assert drafts.content_digest(draft_of(attachments=[str(path)], attempts=1)) == first
    for change in ({"to": ["eve@example.org"]}, {"cc": ["c@example.org"]},
                   {"bcc": ["b@example.org"]}, {"subject": "Ho"}, {"body": "Hello!"},
                   {"mailbox": "gmail"}, {"in_reply_to": "<x@y.z>"}, {"references": "<x@y.z>"},
                   {"forward_key": "qq/inbox/5"}):
        assert drafts.content_digest(draft_of(attachments=[str(path)], **change)) != first
    path.write_bytes(b"two")
    assert drafts.content_digest(d) != first
    expected = hashlib.sha256(json.dumps({
        "mailbox": "qq", "to": ["bob@example.org"], "cc": [], "bcc": [], "subject": "Hi",
        "body": "Hello", "in_reply_to": "", "references": "", "forward_key": "",
        "attachments": [[str(path), hashlib.sha256(b"two").hexdigest()]]},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert drafts.content_digest(d) == expected


def test_load_refuses_a_damaged_draft(mailhub):
    ctx = load(mailhub.root)
    d = draft_of()
    drafts.save(ctx, d)
    assert drafts.load(ctx, d.id) == d
    path = mailhub.state / "drafts" / f"{d.id}.json"
    data = json.loads(path.read_text("utf-8"))
    path.write_text(json.dumps({**data, "to": "bob@example.org"}), "utf-8")
    with pytest.raises(MailError, match=f"draft {d.id} is damaged"):
        drafts.load(ctx, d.id)
    assert drafts.pending(ctx) == []


def test_the_strings_of_drafts_exist():
    for key in ("code_line", "quote_header", "show_bcc", "draft_forwarded"):
        assert strings.t(key, "en", code="1", id="d", date="x", sender="y", value="v")
        assert strings.t(key, "zh", code="1", id="d", date="x", sender="y", value="v")

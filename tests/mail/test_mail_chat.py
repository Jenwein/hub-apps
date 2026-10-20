"""The private chat (mailkit.chat, mailkit.strings, `mx chat-set`, `mx chat-show`).

`cc-connect` is the fake of fakes.py (FAKE_CC_CONNECT); the real one is never run here.
"""

import json

import pytest
from mailkit import chat as chat_module
from mailkit import strings
from mailkit.chat import MAX_PART, Chat, ChatError, load, record, send_file, send_text, split
from mailkit.cli import main
from mailkit.context import load as load_ctx
from mailkit.settings import MailError
from mailtest import CHAT_PROJECT, CHAT_SESSION, record_chat

SEND = ["send", "-p", CHAT_PROJECT, "-s", CHAT_SESSION]


@pytest.fixture
def chat(hub):
    record_chat(hub)
    return Chat(CHAT_PROJECT, CHAT_SESSION)


def run(hub, capsys, *argv: str) -> tuple[int, str, str]:
    code = main(["--hub", str(hub.root), *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def set_language(hub, tag: str) -> None:
    (hub.data / "config.toml").write_text(f'[chat]\nlanguage = "{tag}"\n', "utf-8", newline="\n")


# --- the texts ---------------------------------------------------------------------------------


def test_lang_is_zh_for_chinese_tags_else_en():
    chinese = ("zh", "zh-CN", "zh_TW", "ZH-hans")
    other = ("en", "en-US", "", "de")
    assert [strings.lang(tag) for tag in (*chinese, *other)] == ["zh"] * 4 + ["en"] * 4


def test_every_text_exists_in_both_languages():
    assert set(strings.STRINGS) == {"en", "zh"}
    assert set(strings.STRINGS["en"]) == set(strings.STRINGS["zh"])


def test_t_takes_a_language_tag_and_fills_in_values():
    assert strings.t("answer_about", "en", subject="Hi {x}") == 'About "Hi {x}":'
    assert strings.t("answer_about", "zh-CN", subject="你好") == "关于“你好”："
    assert strings.t("chat_hello", "") == strings.STRINGS["en"]["chat_hello"]
    assert strings.t("chat_hello", "zh") == strings.STRINGS["zh"]["chat_hello"]
    with pytest.raises(KeyError):
        strings.t("no_such_text", "en")


# --- recording the chat ------------------------------------------------------------------------


def test_chat_set_needs_a_session_key(hub, monkeypatch, capsys):
    monkeypatch.delenv("CC_SESSION_KEY", raising=False)
    monkeypatch.setenv("CC_PROJECT", "proj")

    code, out, err = run(hub, capsys, "chat-set")

    assert (code, out) == (1, "")
    assert err == (
        "mx: run this from the agent in the user's private chat: CC_SESSION_KEY is not set\n")
    assert not (hub.state / "chat.json").exists()
    with pytest.raises(MailError, match="CC_SESSION_KEY is not set"):
        record(load_ctx(hub.root), {"CC_SESSION_KEY": "  ", "CC_PROJECT": "proj"})


def test_chat_set_records_and_says_hello(hub, fake_chat, monkeypatch, capsys):
    monkeypatch.setenv("CC_SESSION_KEY", CHAT_SESSION)
    monkeypatch.setenv("CC_PROJECT", CHAT_PROJECT)

    code, out, err = run(hub, capsys, "chat-set")

    assert (code, err) == (0, "")
    assert json.loads(out) == {"chat": {"project": CHAT_PROJECT, "session": CHAT_SESSION}}
    assert json.loads((hub.state / "chat.json").read_text("utf-8")) == {
        "project": CHAT_PROJECT, "session": CHAT_SESSION}
    [call] = fake_chat.calls()
    assert call["argv"][1:] == [*SEND, "--stdin"]
    assert call["stdin"] == strings.t("chat_hello", "")
    # The greeting says where drafts, codes and alerts come from now on.
    assert all(word in call["stdin"] for word in ("drafts", "confirmation codes", "alerts"))

    code, out, err = run(hub, capsys, "chat-show")
    assert (code, err) == (0, "")
    assert json.loads(out) == {"chat": {"project": CHAT_PROJECT, "session": CHAT_SESSION}}


def test_chat_set_greets_in_the_users_language(hub, fake_chat, monkeypatch, capsys):
    set_language(hub, "zh-CN")
    monkeypatch.setenv("CC_SESSION_KEY", CHAT_SESSION)
    monkeypatch.setenv("CC_PROJECT", CHAT_PROJECT)

    assert run(hub, capsys, "chat-set")[0] == 0
    [call] = fake_chat.calls()
    assert call["stdin"] == strings.STRINGS["zh"]["chat_hello"]


def test_chat_set_without_a_project_sends_without_p(hub, fake_chat, monkeypatch, capsys):
    monkeypatch.setenv("CC_SESSION_KEY", CHAT_SESSION)
    monkeypatch.delenv("CC_PROJECT", raising=False)

    code, out, err = run(hub, capsys, "chat-set")

    assert (code, err) == (0, "")
    assert json.loads(out) == {"chat": {"project": None, "session": CHAT_SESSION}}
    [call] = fake_chat.calls()
    assert call["argv"][1:] == ["send", "-s", CHAT_SESSION, "--stdin"]


def test_chat_set_replaces_the_chat_recorded_before(hub, fake_chat, monkeypatch, capsys):
    record_chat(hub, project="old", session="feishu:old")
    monkeypatch.setenv("CC_SESSION_KEY", "feishu:new")
    monkeypatch.setenv("CC_PROJECT", "new")

    assert run(hub, capsys, "chat-set")[0] == 0
    capsys.readouterr()
    assert load(load_ctx(hub.root)) == Chat("new", "feishu:new")


def test_chat_set_with_cc_connect_down_says_so(hub, fake_chat, monkeypatch, capsys):
    fake_chat.mode("fail")
    monkeypatch.setenv("CC_SESSION_KEY", CHAT_SESSION)

    code, out, err = run(hub, capsys, "chat-set")

    assert (code, out) == (1, "")
    assert err.startswith("mx: cc-connect exited with status 1")
    assert "boom: cc-connect is not connected" in err


def test_chat_show_before_chat_set_is_null(hub, capsys):
    code, out, err = run(hub, capsys, "chat-show")
    assert (code, err) == (0, "")
    assert json.loads(out) == {"chat": None}


def test_load_without_a_chat_or_with_a_broken_file(hub):
    ctx = load_ctx(hub.root)
    message = "no private chat recorded: run mx chat-set in the private chat"
    with pytest.raises(MailError) as missing:
        load(ctx)
    assert str(missing.value) == message
    for broken in ('{"session": ""}', '{"session": 5}', '{"project": 3, "session": "a"}',
                   "[]", "null"):
        (hub.state / "chat.json").write_text(broken, "utf-8")
        with pytest.raises(MailError) as bad:
            load(ctx)
        assert str(bad.value) == message


# --- sending -----------------------------------------------------------------------------------


def test_send_uses_stdin_and_the_recorded_chat(hub, fake_chat, chat):
    text = 'Hi 你好 & %PATH% ^ "quoted" \'single\'\n\nline two\n  indented'

    send_text(load(load_ctx(hub.root)), text)

    [call] = fake_chat.calls()
    assert call["argv"][1:] == [*SEND, "--stdin"]
    assert call["stdin"] == text
    # The text is not in the arguments, however it is written.
    assert all(word not in call["argv"] for word in text.split())
    assert text not in " ".join(call["argv"])


def test_send_file_passes_the_path_and_no_stdin(hub, fake_chat, chat, tmp_path):
    path = tmp_path / "the file.pdf"
    path.write_bytes(b"%PDF-1.4\n")

    send_file(chat, path)

    [call] = fake_chat.calls()
    assert call["argv"][1:] == [*SEND, "--file", str(path)]
    assert call["stdin"] is None
    assert call["files"] == {str(path): "JVBERi0xLjQK"}


def test_send_without_a_project_leaves_out_p(fake_chat):
    send_text(Chat(None, "feishu:abc"), "hello")
    [call] = fake_chat.calls()
    assert call["argv"][1:] == ["send", "-s", "feishu:abc", "--stdin"]


def test_a_lone_surrogate_in_the_text_does_not_crash_the_send(fake_chat, chat):
    send_text(chat, "before \udcff after")
    assert fake_chat.messages() == ["before ? after"]


def test_blank_text_sends_nothing(fake_chat, chat):
    send_text(chat, "")
    send_text(chat, " \n\n  ")
    assert fake_chat.calls() == []


def test_long_text_split_at_lines_max_4000():
    text = "".join(f"{n:02d}" + "x" * 97 + "\n" for n in range(90))  # 90 lines of 100 characters
    assert len(text) == 9000

    parts = split(text)

    assert [len(part) for part in parts] == [4000, 4000, 1000]
    assert "".join(parts) == text
    assert all(part.endswith("\n") for part in parts)  # every cut is at a line break
    assert all(part.startswith(f"{n:02d}") for part, n in zip(parts, (0, 40, 80), strict=True))
    assert MAX_PART == 4000


def test_split_keeps_short_text_whole_and_loses_nothing():
    assert split("") == []
    assert split("one line") == ["one line"]
    assert split("a\n\nb\n") == ["a\n\nb\n"]
    text = "\n".join(f"line {n} 你好" for n in range(2000))
    for limit in (7, 100, 4000):
        parts = split(text, limit)
        assert "".join(parts) == text
        assert all(0 < len(part) <= limit for part in parts)


def test_split_cuts_a_line_longer_than_a_part_hard_and_goes_on_with_the_next():
    text = "head\n" + "y" * 9000 + "\ntail\n"

    parts = split(text, 4000)

    assert parts == ["head\n", "y" * 4000, "y" * 4000, "y" * 1000 + "\ntail\n"]
    assert "".join(parts) == text
    exact = "z" * 8000
    assert split(exact, 4000) == ["z" * 4000, "z" * 4000]


def test_send_text_sends_every_part_in_order(hub, fake_chat, chat):
    lines = [f"line {n:04d} " + "w" * 40 for n in range(300)]  # about 15000 characters
    text = "\n".join(lines) + "\n"

    send_text(chat, text)

    messages = fake_chat.messages()
    assert len(messages) == 4
    assert all(len(message) <= MAX_PART for message in messages)
    assert "".join(messages) == text
    assert [call["argv"][1:] for call in fake_chat.calls()] == [[*SEND, "--stdin"]] * 4


# --- errors ------------------------------------------------------------------------------------

SECRET_TEXT = "The code is 482913 and the password is hunter2hunter2"


def test_chat_error_never_contains_the_text(hub, fake_chat, chat):
    fake_chat.mode("fail")
    with pytest.raises(ChatError) as failed:
        send_text(chat, SECRET_TEXT)
    assert str(failed.value) == "cc-connect exited with status 1: boom: cc-connect is not connected"
    assert isinstance(failed.value, MailError)

    # cc-connect that repeats what it was given: the error says it did and shows none of it.
    fake_chat.mode("echo")
    with pytest.raises(ChatError) as echoed:
        send_text(chat, SECRET_TEXT)
    assert "482913" not in str(echoed.value) and "hunter2" not in str(echoed.value)
    assert "cannot send" not in str(echoed.value)
    assert str(echoed.value).startswith("cc-connect exited with status 1")


def test_chat_error_cuts_cc_connects_message_to_200_characters(fake_chat, chat):
    fake_chat.mode("loud")
    with pytest.raises(ChatError) as failed:
        send_text(chat, "hello")
    prefix = "cc-connect exited with status 1: "
    assert str(failed.value).startswith(prefix)
    assert 0 < len(str(failed.value)) - len(prefix) <= 200


def test_a_failure_in_the_middle_names_the_part_and_stops(fake_chat, chat):
    fake_chat.fail_from(2)
    text = "".join(f"{n:02d}" + "x" * 97 + "\n" for n in range(90))

    with pytest.raises(ChatError) as failed:
        send_text(chat, text)

    assert str(failed.value).startswith("cc-connect exited with status 1 (part 2 of 3): boom")
    assert len(fake_chat.messages()) == 1  # part 1 arrived
    assert len(fake_chat.calls()) == 2  # part 3 was not tried
    assert text[4000:4020] not in str(failed.value)


def test_a_missing_cc_connect_is_a_chat_error(chat, monkeypatch, tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    for send in (lambda: send_text(chat, "hello"), lambda: send_file(chat, tmp_path)):
        with pytest.raises(ChatError, match="cc-connect is not on PATH"):
            send()


def test_cc_connect_that_does_not_answer_is_stopped(fake_chat, chat, monkeypatch):
    fake_chat.mode("sleep")
    monkeypatch.setattr(chat_module, "SEND_TIMEOUT", 1)
    with pytest.raises(ChatError, match=r"cc-connect did not answer within 1 s"):
        send_text(chat, "hello")

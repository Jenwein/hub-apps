import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from mailkit import cli
from mailkit.config import (
    DEFAULT_IMPORTANT,
    Mailbox,
    MailError,
    add_mailbox_text,
    effective,
    load,
    parse,
    read_text,
    remove_mailbox_text,
    rename_mailbox_text,
    set_mailbox_value_text,
    write_text,
)

ROOT = Path(__file__).parents[1]
SEED_MAIL = ROOT / "mail" / "seed" / "mail"
MK = ROOT / "mail" / "scripts" / "mk.py"


def write(path: Path, text: str) -> None:
    path.write_text(text, "utf-8", newline="\n")


def work_box(**extra) -> Mailbox:
    fields = {
        "name": "工作", "address": "me+x@corp.example", "provider": "feishu",
        "imap": "imap.feishu.cn:993", "smtp": "smtp.feishu.cn:465",
        "added": "2026-10-14T10:00:00+08:00",
    }
    return Mailbox(**{**fields, **extra})


# A config as a person might leave it: comments everywhere, every kind of header spelling.
HAND = """\
# my mail
[settings]
important = "mine"          # one sentence
delivery = "instant"
muted_senders = []
notes = []

# ---- work ----
[mailboxes."工作"]
address = "me+x@corp.example"
provider = "feishu"
imap = "imap.feishu.cn:993"
smtp = "smtp.feishu.cn:465"
added = "2026-10-14T10:00:00+08:00"
delivery = "instant"   # push at once
notes = [
  "first",   # keep
  "second",
]

# ---- personal ----
[mailboxes.QQ]
address = "me@qq.com"
provider = "qq"
imap = "imap.qq.com:993"
smtp = "smtp.qq.com:465"
added = "2026-10-14T11:00:00+08:00"
"""


def test_seed_config_loads_with_defaults():
    s = load(SEED_MAIL)
    assert (s.important, s.delivery, s.mailboxes) == (DEFAULT_IMPORTANT, "instant", {})
    assert (s.muted_senders, s.notes) == ([], [])


def test_seed_config_says_who_edits_it_and_where_the_times_live():
    text = (SEED_MAIL / "config.toml").read_text("utf-8")
    assert "hub.toml" in text and "cc-hub cron set mail/poll" in text and "mail/digest" in text


def test_mailbox_section_round_trip(tmp_path):
    text = (SEED_MAIL / "config.toml").read_text("utf-8")
    mb = Mailbox(
        "工作", "me+x@corp.example", "feishu", "imap.feishu.cn:993", "smtp.feishu.cn:465",
        "2026-10-14T10:00:00+08:00", muted_senders=[], notes=[],
    )
    out = add_mailbox_text(text, mb)
    assert out.startswith(text)  # comments and settings untouched
    write(tmp_path / "config.toml", out)
    assert load(tmp_path).mailboxes["工作"].address == "me+x@corp.example"
    out2 = set_mailbox_value_text(out, "工作", "paused", True)
    write(tmp_path / "config.toml", out2)
    assert load(tmp_path).mailboxes["工作"].paused
    assert remove_mailbox_text(out2, "工作") == text


def test_every_field_survives_add_and_load(tmp_path):
    mb = work_box(
        paused=True, important="Only my boss.", delivery="digest",
        muted_senders=["a@b.example", "@ads.example"], notes=["one", "two"],
    )
    seed = (SEED_MAIL / "config.toml").read_text("utf-8")
    write(tmp_path / "config.toml", add_mailbox_text(seed, mb))
    assert load(tmp_path).mailboxes == {"工作": mb}


NASTY = [
    'say "hi"', "back\\slash", "tab\there", "line\nbreak", "# not a comment", "] [",
    "中文 ✉ emoji 🎉", "ctrl\x01char\x7f", "ends with a backslash\\",
]


def test_awkward_text_round_trips_through_add_and_set(tmp_path):
    seed = (SEED_MAIL / "config.toml").read_text("utf-8")
    mb = work_box(notes=NASTY, important=NASTY[0])
    out = add_mailbox_text(seed, mb)
    write(tmp_path / "config.toml", out)
    assert load(tmp_path).mailboxes["工作"] == mb
    out = set_mailbox_value_text(out, "工作", "notes", list(reversed(NASTY)))
    out = set_mailbox_value_text(out, "工作", "important", NASTY[-1])
    write(tmp_path / "config.toml", out)
    got = load(tmp_path).mailboxes["工作"]
    assert (got.notes, got.important) == (list(reversed(NASTY)), NASTY[-1])


def test_effective_rules_override_and_union(tmp_path):
    text = """\
[settings]
important = "user rule"
delivery = "instant"
muted_senders = ["@ads.example"]
notes = ["a"]

[mailboxes."工作"]
address = "w@corp.example"
provider = "feishu"
imap = "imap.feishu.cn:993"
smtp = "smtp.feishu.cn:465"
added = "2026-10-14T10:00:00+08:00"
delivery = "digest"
muted_senders = ["x@y", "@ads.example"]
notes = ["b"]
paused = true

[mailboxes."QQ"]
address = "me@qq.com"
provider = "qq"
imap = "imap.qq.com:993"
smtp = "smtp.qq.com:465"
added = "2026-10-14T11:00:00+08:00"
important = "QQ rule"
"""
    write(tmp_path / "config.toml", text)
    s = load(tmp_path)
    assert effective(s, "工作") == {
        "important": "user rule", "delivery": "digest", "muted_senders": ["x@y", "@ads.example"],
        "notes": ["b", "a"], "paused": True,
    }
    assert effective(s, "QQ") == {
        "important": "QQ rule", "delivery": "instant", "muted_senders": ["@ads.example"],
        "notes": ["a"], "paused": False,
    }
    got = effective(s, "QQ")
    got["notes"].append("changed")  # a copy: the settings are not touched
    assert s.notes == ["a"] and s.mailboxes["QQ"].notes == []
    with pytest.raises(MailError, match="nope"):
        effective(s, "nope")


@pytest.mark.parametrize("bad, key", [
    ('delivery = "sometimes"', "delivery"),
    ('muted_senders = "x"', "muted_senders"),
    ("notes = [1]", "notes"),
    ('notes = ["fine", " "]', "notes"),
    ('important = ""', "important"),
    ('delivry = "digest"', "delivry"),
])
def test_bad_values_are_named(tmp_path, bad, key):
    write(tmp_path / "config.toml", f"[settings]\n{bad}\n")
    with pytest.raises(MailError, match=key):
        load(tmp_path)


def mailbox_with(line: str) -> str:
    """The QQ mailbox of HAND with the key of `line` set to it (or added)."""
    key = line.partition(" =")[0]
    body = HAND.partition("[mailboxes.QQ]\n")[2].splitlines()
    kept = [ln for ln in body if not ln.startswith(key)]
    return "[mailboxes.QQ]\n" + "\n".join([*kept, line]) + "\n"


@pytest.mark.parametrize("line, key", [
    ('address = "nobody"', "address"),
    ('address = 7', "address"),
    ('provider = ""', "provider"),
    ('imap = "imap.qq.com"', "imap"),
    ('smtp = "smtp.qq.com:99999"', "smtp"),
    ('smtp = "smtp.qq.com:abc"', "smtp"),
    ('added = "yesterday"', "added"),
    ('added = "2026-10-14T10:00:00"', "added"),
    ("added = 2026-10-14", "added"),
    ('paused = "yes"', "paused"),
    ('delivery = "sometimes"', "delivery"),
    ('colour = "red"', "colour"),
])
def test_bad_mailbox_values_are_named(tmp_path, line, key):
    write(tmp_path / "config.toml", mailbox_with(line))
    with pytest.raises(MailError, match=key) as err:
        load(tmp_path)
    assert "QQ" in str(err.value)


def test_a_missing_mailbox_key_is_named(tmp_path):
    text = mailbox_with('paused = true').replace('imap = "imap.qq.com:993"\n', "")
    write(tmp_path / "config.toml", text)
    with pytest.raises(MailError, match="imap"):
        load(tmp_path)


def test_other_broken_files_are_mail_errors(tmp_path):
    with pytest.raises(MailError, match="config.toml"):
        load(tmp_path)  # no file
    (tmp_path / "config.toml").write_bytes(b"[settings]\nnotes = ['\xff']\n")
    with pytest.raises(MailError, match="UTF-8"):
        load(tmp_path)
    write(tmp_path / "config.toml", "[settings\n")
    with pytest.raises(MailError, match="TOML"):
        load(tmp_path)
    write(tmp_path / "config.toml", "[other]\nx = 1\n")
    with pytest.raises(MailError, match="other"):
        load(tmp_path)
    write(tmp_path / "config.toml", "mailboxes = 1\n")
    with pytest.raises(MailError, match="mailboxes"):
        load(tmp_path)


def test_a_time_written_without_quotes_is_read_as_the_same_time(tmp_path):
    bare = HAND.replace('"2026-10-14T11:00:00+08:00"', "2026-10-14T11:00:00+08:00")
    assert bare != HAND
    write(tmp_path / "config.toml", bare)
    assert load(tmp_path).mailboxes["QQ"].added == "2026-10-14T11:00:00+08:00"


def test_a_byte_order_mark_is_tolerated(tmp_path):
    (tmp_path / "config.toml").write_bytes(b"\xef\xbb\xbf" + HAND.encode("utf-8"))
    assert list(load(tmp_path).mailboxes) == ["工作", "QQ"]
    out = set_mailbox_value_text(read_text(tmp_path), "QQ", "paused", True)
    assert out.startswith("﻿# my mail")


# ---- the section editors keep every other byte ----


def test_set_replaces_a_value_and_keeps_its_comment():
    out = set_mailbox_value_text(HAND, "工作", "delivery", "digest")
    assert out == HAND.replace(
        'delivery = "instant"   # push at once', 'delivery = "digest"   # push at once')


def test_set_adds_a_missing_key_after_the_last_one_of_the_section():
    out = set_mailbox_value_text(HAND, "工作", "paused", True)
    assert out == HAND.replace("]\n\n# ---- personal", "]\npaused = true\n\n# ---- personal")
    out = set_mailbox_value_text(HAND, "QQ", "muted_senders", ["@ads.example"])
    assert out == HAND + 'muted_senders = [\n  "@ads.example",\n]\n'


def test_set_replaces_a_list_written_over_several_lines():
    out = set_mailbox_value_text(HAND, "工作", "notes", ["only"])
    assert out == HAND.replace(
        'notes = [\n  "first",   # keep\n  "second",\n]\n', 'notes = [\n  "only",\n]\n')
    out = set_mailbox_value_text(HAND, "工作", "notes", [])
    assert out == HAND.replace('notes = [\n  "first",   # keep\n  "second",\n]\n', "notes = []\n")


def test_set_none_removes_an_optional_key_and_refuses_a_required_one():
    out = set_mailbox_value_text(HAND, "工作", "delivery", None)
    assert out == HAND.replace('delivery = "instant"   # push at once\n', "")
    assert set_mailbox_value_text(HAND, "QQ", "delivery", None) == HAND  # nothing to remove
    with pytest.raises(MailError, match="address"):
        set_mailbox_value_text(HAND, "QQ", "address", None)


def test_set_checks_the_value_and_the_key():
    with pytest.raises(MailError, match="delivery"):
        set_mailbox_value_text(HAND, "QQ", "delivery", "sometimes")
    with pytest.raises(MailError, match="paused"):
        set_mailbox_value_text(HAND, "QQ", "paused", "yes")
    with pytest.raises(MailError, match="colour"):
        set_mailbox_value_text(HAND, "QQ", "colour", "red")
    with pytest.raises(MailError, match="nope"):
        set_mailbox_value_text(HAND, "nope", "paused", True)


@pytest.mark.parametrize("header", [
    '[mailboxes."工作"]', "[mailboxes.'工作']", '[ mailboxes . "工作" ]  # work',
])
def test_every_spelling_of_a_header_is_found(header):
    text = (
        f'[settings]\nnotes = []\n\n{header}\naddress = "me+x@corp.example"\n'
        'provider = "feishu"\nimap = "imap.feishu.cn:993"\nsmtp = "smtp.feishu.cn:465"\n'
        'added = "2026-10-14T10:00:00+08:00"\n'
    )
    out = set_mailbox_value_text(text, "工作", "paused", True)
    assert out == text + "paused = true\n"
    assert remove_mailbox_text(out, "工作") == "[settings]\nnotes = []\n"


def test_remove_takes_the_section_and_leaves_the_comments_around_it():
    out = remove_mailbox_text(HAND, "QQ")  # a bare name, the last section
    assert out == HAND[: HAND.index("[mailboxes.QQ]")]
    work = HAND[HAND.index('[mailboxes."工作"]') : HAND.index("\n# ---- personal")]
    assert work.endswith("]\n")
    assert remove_mailbox_text(HAND, "工作") == HAND.replace(work, "")


def test_the_last_section_without_a_final_newline():
    text = HAND.rstrip("\n")
    out = set_mailbox_value_text(text, "QQ", "paused", True)
    assert out == text + "\npaused = true\n"
    assert remove_mailbox_text(text, "QQ") == HAND[: HAND.index("[mailboxes.QQ]")]


def test_add_refuses_a_name_that_is_taken_and_a_bad_mailbox():
    with pytest.raises(MailError, match="工作"):
        add_mailbox_text(HAND, work_box())
    for bad, key in (
        (work_box(name="Y", delivery="sometimes"), "delivery"),
        (work_box(name="Y", imap="nope"), "imap"),
        (work_box(name="Y", address="x"), "address"),
        (work_box(name="Y", notes="x"), "notes"),
    ):
        with pytest.raises(MailError, match=key):
            add_mailbox_text(HAND, bad)


@pytest.mark.parametrize("name", ["", 'a"b', "a\\b", "a\nb", "a\x7fb"])
def test_names_that_toml_cannot_hold_are_refused(name):
    with pytest.raises(MailError, match="name"):
        add_mailbox_text(HAND, work_box(name=name))
    with pytest.raises(MailError, match="name"):
        set_mailbox_value_text(HAND, name, "paused", True)
    with pytest.raises(MailError, match="name"):
        remove_mailbox_text(HAND, name)


def test_a_section_that_cannot_be_edited_in_place_is_refused_not_guessed():
    inline = (
        '[mailboxes]\n"工作" = { address = "a@b.example", provider = "qq", '
        'imap = "i:1", smtp = "s:2", added = "2026-10-14T10:00:00+08:00" }\n'
    )
    for call in (
        lambda: set_mailbox_value_text(inline, "工作", "paused", True),
        lambda: remove_mailbox_text(inline, "工作"),
    ):
        with pytest.raises(MailError, match="by hand"):
            call()
    with pytest.raises(MailError, match="工作"):
        add_mailbox_text(inline, work_box())
    with pytest.raises(MailError, match="TOML"):
        set_mailbox_value_text("[mailboxes\n", "工作", "paused", True)


def test_a_value_the_scanner_cannot_delimit_is_refused_not_cut_in_half():
    # the second line of the note starts with [, so it looks like the next section's header
    text = HAND.replace(
        'notes = [\n  "first",   # keep\n  "second",\n]\n', 'notes = ["""a\n[b]"""]\n')
    assert text != HAND
    assert parse(text).mailboxes["工作"].notes == ["a\n[b]"]
    with pytest.raises(MailError, match="by hand"):
        set_mailbox_value_text(text, "工作", "paused", True)


def test_an_edit_that_changes_anything_else_is_refused():
    # a note whose text holds a line that looks like the header of 工作: the edit would land in it
    fake = '[settings]\nnotes = ["""x\n[mailboxes."工作"]\n# y"""]\n\n'
    text = fake + HAND[HAND.index('[mailboxes."工作"]') :]
    assert parse(text).mailboxes["工作"].address == "me+x@corp.example"
    for call in (
        lambda: set_mailbox_value_text(text, "工作", "paused", True),
        lambda: remove_mailbox_text(text, "工作"),
    ):
        with pytest.raises(MailError, match="by hand"):
            call()


def test_crlf_files_stay_crlf(tmp_path):
    (tmp_path / "config.toml").write_bytes(HAND.replace("\n", "\r\n").encode("utf-8"))
    text = read_text(tmp_path)
    assert "\r\n" in text
    text = add_mailbox_text(text, work_box(name="Gmail", address="me@gmail.com"))
    text = set_mailbox_value_text(text, "工作", "delivery", "digest")
    text = set_mailbox_value_text(text, "QQ", "paused", True)
    write_text(tmp_path, text)
    raw = (tmp_path / "config.toml").read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n")
    assert list(load(tmp_path).mailboxes) == ["工作", "QQ", "Gmail"]
    text = remove_mailbox_text(remove_mailbox_text(read_text(tmp_path), "Gmail"), "QQ")
    write_text(tmp_path, text)
    raw = (tmp_path / "config.toml").read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n") and b"QQ" not in raw


def test_rename_changes_only_the_header():
    out = rename_mailbox_text(HAND, "工作", "Work")
    assert out == HAND.replace('[mailboxes."工作"]', '[mailboxes."Work"]')
    assert list(parse(out).mailboxes) == ["Work", "QQ"]
    assert parse(out).mailboxes["Work"].notes == parse(HAND).mailboxes["工作"].notes
    assert rename_mailbox_text(HAND, "工作", "工作") == HAND


@pytest.mark.parametrize("header,renamed", [
    ("[mailboxes.'工作']", '[mailboxes."QQ 2"]'),
    ('  [ mailboxes . "工作" ]  # work', '  [mailboxes."QQ 2"]  # work'),
    ("[mailboxes.work-1]", '[mailboxes."QQ 2"]'),
])
def test_rename_keeps_indent_and_comment_of_any_header_spelling(header, renamed):
    name = "work-1" if "work-1" in header else "工作"
    text = (
        f'[settings]\nnotes = []\n\n{header}\r\naddress = "me+x@corp.example"\n'
        'provider = "feishu"\nimap = "imap.feishu.cn:993"\nsmtp = "smtp.feishu.cn:465"\n'
        'added = "2026-10-14T10:00:00+08:00"\n'
    )
    out = rename_mailbox_text(text, name, "QQ 2")
    assert out == text.replace(header, renamed)
    assert list(parse(out).mailboxes) == ["QQ 2"]


def test_rename_refusals():
    with pytest.raises(MailError, match="already a mailbox named"):
        rename_mailbox_text(HAND, "工作", "QQ")
    with pytest.raises(MailError, match="no mailbox named"):
        rename_mailbox_text(HAND, "nope", "x")
    with pytest.raises(MailError, match="name"):
        rename_mailbox_text(HAND, "工作", 'a"b')
    inline = (
        '[mailboxes]\n"工作" = { address = "a@b.example", provider = "qq", '
        'imap = "i:1", smtp = "s:2", added = "2026-10-14T10:00:00+08:00" }\n'
    )
    with pytest.raises(MailError, match="by hand"):
        rename_mailbox_text(inline, "工作", "x")
    fake = '[settings]\nnotes = ["""x\n[mailboxes."工作"]\n# y"""]\n\n'
    text = fake + HAND[HAND.index('[mailboxes."工作"]') :]
    with pytest.raises(MailError, match="by hand"):
        rename_mailbox_text(text, "工作", "x")


def test_write_text_replaces_the_file_at_once(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_bytes(b"[settings]\r\nnotes = []\r\n")
    if os.name != "nt":
        os.chmod(tmp_path / "config.toml", 0o640)

    def failing(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", failing)
    with pytest.raises(MailError, match="disk full"):
        write_text(tmp_path, "[settings]\nnotes = []\n")
    assert (tmp_path / "config.toml").read_bytes() == b"[settings]\r\nnotes = []\r\n"
    assert [p.name for p in tmp_path.iterdir()] == ["config.toml"]  # no temp file left
    monkeypatch.undo()
    write_text(tmp_path, '[settings]\r\nnotes = ["x"]\r\n')
    assert (tmp_path / "config.toml").read_bytes() == b'[settings]\r\nnotes = ["x"]\r\n'
    assert [p.name for p in tmp_path.iterdir()] == ["config.toml"]
    if os.name != "nt":
        assert (tmp_path / "config.toml").stat().st_mode & 0o777 == 0o640


# ---- the command line ----


def test_mk_config_check(tmp_path, capsys):
    shutil.copytree(SEED_MAIL, tmp_path / "mail")
    assert cli.main(["--mail-dir", str(tmp_path / "mail"), "config", "check"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "mailboxes": []}


def test_mk_config_check_lists_the_mailboxes(tmp_path, capsys):
    write(tmp_path / "config.toml", HAND)
    assert cli.main(["--mail-dir", str(tmp_path), "config", "check"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "mailboxes": ["工作", "QQ"]}


def test_mk_mail_dir_defaults_to_mail_in_the_working_directory(tmp_path, capsys, monkeypatch):
    shutil.copytree(SEED_MAIL, tmp_path / "mail")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["config", "check"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_mk_errors_are_one_line_on_stderr_and_exit_1(tmp_path, capsys):
    write(tmp_path / "config.toml", '[settings]\ndelivery = "sometimes"\n')
    assert cli.main(["--mail-dir", str(tmp_path), "config", "check"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("mk: ") and "delivery" in captured.err
    assert captured.err.count("\n") == 1
    for argv in ([], ["config"], ["bogus"], ["config", "check", "--nope"]):
        assert cli.main(argv) == 1
        assert capsys.readouterr().err.rstrip().splitlines()[-1].startswith("mk: ")


def test_mk_script_prints_utf8_whatever_the_console_encoding(tmp_path):
    write(tmp_path / "config.toml", HAND)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    done = subprocess.run(
        [sys.executable, str(MK), "--mail-dir", str(tmp_path), "config", "check"],
        capture_output=True, env=env, check=False, cwd=tmp_path,
    )
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    assert json.loads(done.stdout.decode("utf-8"))["mailboxes"] == ["工作", "QQ"]

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest
from conftest import ROOT, SEED, write_mailboxes
from mailkit import cli, context, paths
from mailkit.cli import main
from mailkit.settings import (
    FOLDER_ROLES,
    NAME_RE,
    PROVIDERS,
    MailError,
    Provider,
    Server,
    Settings,
    load_mailboxes,
    load_settings,
)

POSIX = os.name == "posix"

QQ_AND_GMAIL = """\
[[mailbox]]
name = "qq"
address = "me@qq.com"
display_name = "Me"
provider = "qq"

[[mailbox]]
name = "gmail"
address = "me@gmail.com"
provider = "gmail"
"""


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- settings ---


def test_seed_files_parse():
    assert load_mailboxes(SEED) == {}
    assert load_settings(SEED) == Settings(False, "", "sonnet", "")


def test_app_toml_fields():
    data = tomllib.loads((ROOT / "mail" / "app.toml").read_text("utf-8"))
    assert data["app"]["name"] == "mail" and data["app"]["version"] == "0.1.0"
    assert data["app"]["min_hub"] == "0.7.0"  # reports/ needs cc-hub 0.7.0
    assert data["data"]["dirs"] == ["mail"]


def test_builtin_providers():
    assert list(PROVIDERS) == ["feishu", "qq", "gmail", "outlook"]
    assert PROVIDERS["feishu"] == Provider(
        "feishu", Server("imap.feishu.cn", 993, "tls"), Server("smtp.feishu.cn", 465, "tls"),
        "password", False, True)
    assert PROVIDERS["outlook"].smtp == Server("smtp-mail.outlook.com", 587, "starttls")
    assert PROVIDERS["outlook"].auth == "oauth"
    assert [p.imap_id for p in PROVIDERS.values()] == [False, True, False, False]
    assert [p.save_copy for p in PROVIDERS.values()] == [True, True, False, True]
    assert FOLDER_ROLES == ("inbox", "sent", "trash", "archive")


def test_provider_defaults(hub):
    write_mailboxes(hub, QQ_AND_GMAIL)
    boxes = load_mailboxes(hub.data)
    assert list(boxes) == ["qq", "gmail"]  # file order
    qq, gmail = boxes["qq"], boxes["gmail"]
    assert qq.imap == Server("imap.qq.com", 993, "tls") and qq.imap_id
    assert qq.smtp == Server("smtp.qq.com", 465, "tls") and qq.auth == "password"
    assert (qq.address, qq.display_name, qq.provider) == ("me@qq.com", "Me", "qq")
    assert qq.save_copy and not gmail.save_copy and gmail.display_name == ""
    assert qq.folders == {"inbox": "INBOX", "sent": "", "trash": "", "archive": ""}


def test_overrides_apply(hub):
    write_mailboxes(hub, """\
[[mailbox]]
name = "local"
address = "a@example.test"
provider = "qq"
save_copy = false
imap_host = "x"
imap_port = 3143
imap_tls = "none"
smtp_host = "y"
smtp_port = 3025
smtp_tls = "starttls"

[mailbox.folders]
sent = "Sent Messages"
archive = "Archive"

[[mailbox]]
name = "port-only"
address = "b@example.test"
provider = "qq"
imap_port = 1993
""")
    boxes = load_mailboxes(hub.data)
    local = boxes["local"]
    assert local.imap == Server("x", 3143, "none") and local.smtp == Server("y", 3025, "starttls")
    assert not local.save_copy and local.imap_id  # the rest still comes from the provider
    assert local.folders == {"inbox": "INBOX", "sent": "Sent Messages", "trash": "",
                             "archive": "Archive"}
    assert boxes["port-only"].imap == Server("imap.qq.com", 1993, "tls")


def entry(**keys) -> str:
    keys = {"name": '"box"', "address": '"a@b.c"', "provider": '"qq"', **keys}
    return "[[mailbox]]\n" + "".join(f"{k} = {v}\n" for k, v in keys.items())


@pytest.mark.parametrize("text, needles", [
    (entry(name='"QQ"'), ["QQ"]),
    (entry(name='"has space"'), ["has space"]),
    (entry(name='"qq\\n"'), ["qq"]),  # `$` must not let a trailing newline through
    (entry(name='"-dash"'), ["-dash"]),
    (entry(name='"' + "a" * 32 + '"'), ["a" * 32]),
    (entry().replace('name = "box"\n', ""), ["#1", "name"]),
    (entry(provider='"163"'), ['"box"', "163", "feishu, qq, gmail, outlook"]),
    (entry().replace('provider = "qq"\n', ""), ['"box"', "provider"]),
    (entry(address='"no-at"'), ['"box"', "no-at"]),
    (entry().replace('address = "a@b.c"\n', ""), ['"box"', "address"]),
    (entry() + entry(), ['"box"', "twice"]),
    (entry(imap_tls='"ssl"'), ['"box"', "imap_tls", "tls, starttls, none"]),
    (entry(smtp_port='"465"'), ['"box"', "smtp_port", "integer"]),
    (entry(smtp_port="0"), ['"box"', "smtp_port"]),
    (entry(imap_host="3"), ['"box"', "imap_host", "string"]),
    (entry(save_copy='"yes"'), ['"box"', "save_copy", "true or false"]),
    (entry(display_name="1"), ['"box"', "display_name", "string"]),
    (entry() + '[mailbox.folders]\nsent = 1\n', ['"box"', "folders.sent", "string"]),
    ('mailbox = "qq"\n', ["[[mailbox]]"]),
    ("[[mailbox]\n", ["cannot read", "mailboxes.toml"]),
], ids=lambda value: value[:30] if isinstance(value, str) else None)
def test_bad_entries_rejected(hub, text, needles):
    write_mailboxes(hub, text)
    with pytest.raises(MailError) as caught:
        load_mailboxes(hub.data)
    for needle in needles:
        assert needle in str(caught.value)


def test_name_pattern():
    assert all(NAME_RE.fullmatch(n) for n in ("qq", "work-2", "0", "a" * 31))
    assert not any(NAME_RE.fullmatch(n) for n in ("", "-a", "A", "a_b", "a.b", "a" * 32))


def test_missing_files_are_errors(tmp_path):
    with pytest.raises(MailError, match="no mailboxes.toml in"):
        load_mailboxes(tmp_path)
    with pytest.raises(MailError, match="no config.toml in"):
        load_settings(tmp_path)


def test_settings_keys_default_to_the_seed_values(tmp_path):
    (tmp_path / "config.toml").write_text("[alerts]\nenabled = true\n", "utf-8")
    assert load_settings(tmp_path) == Settings(True, "", "sonnet", "")
    (tmp_path / "config.toml").write_text(
        '[alerts]\nhost = "box"\n[reader]\nmodel = "haiku"\n[chat]\nlanguage = "zh-CN"\n', "utf-8")
    assert load_settings(tmp_path) == Settings(False, "box", "haiku", "zh-CN")


@pytest.mark.parametrize("text, needle", [
    ('[alerts]\nenabled = "yes"\n', "alerts.enabled must be true or false"),
    ("[reader]\nmodel = 3\n", "reader.model must be a string"),
    ('chat = "zh"\n', "[chat] must be a table"),
    ("[alerts\n", "cannot read"),
])
def test_bad_settings_rejected(tmp_path, text, needle):
    (tmp_path / "config.toml").write_text(text, "utf-8")
    with pytest.raises(MailError, match=re.escape(needle)):
        load_settings(tmp_path)


# --- state directory ---


def test_state_dir_is_per_hub_and_private(tmp_path, monkeypatch):
    monkeypatch.delenv("CC_HUB_MAIL_STATE", raising=False)
    home = tmp_path / "home"
    for name in ("HOME", "USERPROFILE", "LOCALAPPDATA", "XDG_STATE_HOME"):
        monkeypatch.setenv(name, str(home))
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    a, b = paths.state_dir(one), paths.state_dir(two)
    assert a != b and a.is_dir() and b.is_dir()
    assert a.parent == b.parent and a.parent.name == "cc-hub-mail"
    assert re.fullmatch(r"[0-9a-f]{12}", a.name)
    assert paths.state_dir(one) == a
    assert paths.state_dir(tmp_path / "one" / ".." / "one") == a  # the resolved path counts
    if POSIX:
        assert mode(a) == 0o700 and mode(b) == 0o700


@pytest.mark.parametrize("platform, env, expected", [
    ("win32", {"LOCALAPPDATA": "local"}, "local"),
    ("darwin", {}, "home/Library/Application Support"),
    ("linux", {"XDG_STATE_HOME": "xdg"}, "xdg"),
    ("linux", {}, "home/.local/state"),
    ("linux", {"XDG_STATE_HOME": "relative"}, "home/.local/state"),  # XDG: absolute paths only
])
def test_state_dir_base_per_platform(tmp_path, monkeypatch, platform, env, expected):
    monkeypatch.delenv("CC_HUB_MAIL_STATE", raising=False)
    for name in ("LOCALAPPDATA", "XDG_STATE_HOME"):
        monkeypatch.delenv(name, raising=False)
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(tmp_path / "home"))
    for name, value in env.items():
        monkeypatch.setenv(name, value if value == "relative" else str(tmp_path / value))
    monkeypatch.setattr(sys, "platform", platform)
    state = paths.state_dir(tmp_path)
    assert state.parent == tmp_path / expected / "cc-hub-mail"


def test_state_dir_env_override(tmp_path, monkeypatch):
    target = tmp_path / "a" / "state"
    monkeypatch.setenv("CC_HUB_MAIL_STATE", str(target))
    assert paths.state_dir(tmp_path / "any-hub") == target
    assert target.is_dir()
    if POSIX:
        assert mode(target) == 0o700


def test_state_dir_that_cannot_be_made_is_a_mail_error(tmp_path, monkeypatch):
    (tmp_path / "file").write_text("x", "utf-8")
    monkeypatch.setenv("CC_HUB_MAIL_STATE", str(tmp_path / "file" / "state"))
    with pytest.raises(MailError, match="cannot create the mail state directory"):
        paths.state_dir(tmp_path)


# --- private files ---


def test_write_private_is_atomic_and_0600(tmp_path):
    target = tmp_path / "secret"
    paths.write_private(target, b"one")
    paths.write_private(target, "zwei ä".encode())  # replaces
    assert target.read_bytes() == "zwei ä".encode()
    assert list(tmp_path.iterdir()) == [target]  # no temp file left
    if POSIX:
        assert mode(target) == 0o600


def test_write_private_leaves_the_old_file_and_no_temp_when_the_replace_fails(
        tmp_path, monkeypatch):
    target = tmp_path / "secret"
    paths.write_private(target, b"old")

    def refuse(src, dst):
        raise OSError("no")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError):
        paths.write_private(target, b"new")
    assert target.read_bytes() == b"old" and list(tmp_path.iterdir()) == [target]


def test_json_round_trip(tmp_path):
    target = tmp_path / "data.json"
    assert paths.read_json(target, {"none": True}) == {"none": True}
    paths.write_json(target, {"subject": "面试", "n": [1, 2]})
    assert paths.read_json(target, None) == {"subject": "面试", "n": [1, 2]}
    text = target.read_text("utf-8")
    assert "面试" in text and text.endswith("\n")  # not \u-escaped
    if POSIX:
        assert mode(target) == 0o600


def test_read_json_of_a_broken_file_is_a_mail_error(tmp_path):
    target = tmp_path / "data.json"
    target.write_text("{not json", "utf-8")
    with pytest.raises(MailError, match="cannot read"):
        paths.read_json(target, {})
    target.write_bytes(b"\xff\xfe")
    with pytest.raises(MailError, match="cannot read"):
        paths.read_json(target, {})


# --- the lock ---


def test_lock_times_out_and_takes_over_stale(tmp_path):
    lock = tmp_path / "lock"
    with paths.locked(tmp_path):
        assert lock.exists()
        started = time.monotonic()
        with (
            pytest.raises(MailError, match="the mail state is busy"),
            paths.locked(tmp_path, timeout=0.5),
        ):
            pytest.fail("the lock was taken twice")
        assert time.monotonic() - started >= 0.4
        assert lock.exists()  # the failed attempt left the holder's lock alone
    assert not lock.exists()  # released

    lock.write_text("someone", "utf-8")
    old = time.time() - 200
    os.utime(lock, (old, old))
    started = time.monotonic()
    with paths.locked(tmp_path, timeout=0.5):
        assert lock.read_text("utf-8") != "someone"
    assert time.monotonic() - started < 0.4  # at once
    assert not lock.exists()


def test_a_lock_under_two_minutes_old_is_not_stale(tmp_path):
    lock = tmp_path / "lock"
    lock.write_text("someone", "utf-8")
    old = time.time() - 100
    os.utime(lock, (old, old))
    with pytest.raises(MailError, match="busy"), paths.locked(tmp_path, timeout=0.3):
        pytest.fail("the lock was taken over too early")
    assert lock.read_text("utf-8") == "someone"


def test_release_leaves_a_lock_that_another_process_took_over(tmp_path):
    lock = tmp_path / "lock"
    with paths.locked(tmp_path):
        lock.write_text("the new holder", "utf-8")  # taken over after going stale
    assert lock.read_text("utf-8") == "the new holder"


def test_lock_is_released_when_the_body_fails(tmp_path):
    with pytest.raises(RuntimeError), paths.locked(tmp_path):
        raise RuntimeError("boom")
    assert not (tmp_path / "lock").exists()


# --- context ---


def test_context_loads_the_hub(hub):
    write_mailboxes(hub, QQ_AND_GMAIL)
    ctx = context.load(hub.root)
    assert (ctx.hub, ctx.data, ctx.state) == (hub.root, hub.data, hub.state)
    assert ctx.state.is_dir()
    assert ctx.app == context.APP_DIR and (ctx.app / "app.toml").is_file()
    assert ctx.settings == Settings(False, "", "sonnet", "")
    assert list(ctx.mailboxes) == ["qq", "gmail"]
    assert ctx.mailbox("qq") is ctx.mailboxes["qq"]
    with pytest.raises(MailError, match="no mailbox nope; known: qq, gmail"):
        ctx.mailbox("nope")
    assert cli.load_ctx(argparse.Namespace(hub=hub.root)) == ctx


def test_context_without_a_mailbox_file(tmp_path):
    with pytest.raises(MailError) as caught:
        context.load(tmp_path)
    assert str(caught.value) == (
        f"{tmp_path} has no mail/mailboxes.toml: run mx from the hub root")


def test_context_of_a_hub_with_no_mailbox(hub):
    with pytest.raises(MailError, match="no mailbox qq; known: none"):
        context.load(hub.root).mailbox("qq")


def test_context_reports_a_bad_mailbox_file_before_making_the_state_dir(hub):
    write_mailboxes(hub, entry(name='"QQ"'))
    with pytest.raises(MailError, match="QQ"):
        context.load(hub.root)
    assert not hub.state.exists()


# --- command line ---


def test_cli_error_format(tmp_path, capsys):
    assert main(["--hub", str(tmp_path), "secret-status"]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("mx: ") and "secret-status" in captured.err
    assert captured.out == ""


def test_usage_errors_are_mx_errors_with_the_usage_below(capsys):
    assert main([]) == 1
    lines = capsys.readouterr().err.splitlines()
    assert lines[0].startswith("mx: ") and lines[1].startswith("usage: mx ")


def test_print_json(capsys):
    cli.print_json({"subject": "面试", "n": [1]})
    assert capsys.readouterr().out == '{\n  "subject": "面试",\n  "n": [\n    1\n  ]\n}\n'


def test_hub_option_defaults_to_the_current_directory():
    parser = cli.build_parser()
    assert isinstance(parser, cli.Parser)
    assert parser.get_default("hub") == Path()


def test_make_exe_fixture_runs_a_python_script_from_path(make_exe):
    made = make_exe("fake-tool", "import sys\nprint('args:', *sys.argv[1:])\n")
    found = shutil.which("fake-tool")  # a .cmd shim is found through PATHEXT on Windows
    assert found and Path(found) == made
    done = subprocess.run([found, "a b", "c"], capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "args: a b c"

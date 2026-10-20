import getpass
import io
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from mailkit import cli, vault
from mailkit.cli import main
from mailkit.context import APP_DIR, load
from mailkit.paths import this_machine
from mailkit.settings import MailError
from mailkit.tools import NO_WINDOW
from mailkit.vault import (
    exists,
    fetch_command,
    forget,
    mx_command,
    remove,
    secret_path,
    store,
)
from mailtest import ROOT, write_mailboxes

POSIX = os.name == "posix"

TWO_BOXES = """\
[[mailbox]]
name = "qq"
address = "me@qq.com"
provider = "qq"

[[mailbox]]
name = "work"
address = "me@feishu.cn"
provider = "feishu"

[[mailbox]]
name = "outlook"
address = "me@outlook.com"
provider = "outlook"
"""


def terminal(monkeypatch, *entries: str) -> list[str]:
    """Make stdin a terminal and getpass answer the given entries one after the other. Returns the
    prompts that were shown."""
    monkeypatch.setattr(vault, "at_terminal", lambda: True)
    answers = iter(entries)
    prompts: list[str] = []

    def answer(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(getpass, "getpass", answer)
    return prompts


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def boxes(hub):
    write_mailboxes(hub, TWO_BOXES)
    return hub


# --- secret-set ---


def test_secret_set_needs_a_terminal(boxes, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("abcd efgh\nabcd efgh\n"))  # piped, not a tty

    def never(prompt=""):
        raise AssertionError("getpass must not be called without a terminal")

    monkeypatch.setattr(getpass, "getpass", never)
    assert main(["secret-set", "qq"]) == 1
    err = capsys.readouterr().err
    assert err == (
        "mx: type it yourself in the server's terminal: "
        "uv run apps/mail/scripts/mx.py secret-set qq\n")
    assert not exists(boxes.state, "qq")


def test_secret_set_with_no_input_at_all_refuses_instead_of_waiting(boxes):
    """A harness that closes stdin (the NUL device on Windows, which isatty() calls a terminal)
    must get the refusal, not a getpass that waits on the console."""
    argv = [sys.executable, str(ROOT / "mail" / "scripts" / "mx.py"), "--hub", str(boxes.root),
            "secret-set", "qq"]
    env = {**os.environ, "CC_HUB_MAIL_STATE": str(boxes.state)}
    done = subprocess.run(
        argv, check=False, stdin=subprocess.DEVNULL, capture_output=True, env=env,
        creationflags=NO_WINDOW, timeout=20)
    assert done.returncode == 1
    assert b"in the server's terminal" in done.stderr
    assert done.stdout == b""
    assert not exists(boxes.state, "qq")


def test_at_terminal_is_false_for_a_pipe_and_for_no_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("abcd\n"))
    assert vault.at_terminal() is False
    monkeypatch.setattr(sys, "stdin", None)
    assert vault.at_terminal() is False


@pytest.mark.skipif(sys.platform != "win32", reason="the NUL device is a Windows case")
def test_the_nul_device_is_not_a_console():
    fd = os.open(os.devnull, os.O_RDONLY)
    try:
        assert os.isatty(fd) is True  # why isatty() alone is not enough
        assert vault._is_console(fd) is False
    finally:
        os.close(fd)


@pytest.mark.skipif(not POSIX, reason="needs a pseudo-terminal")
def test_at_terminal_is_true_for_a_pty(monkeypatch):
    import pty

    master, slave = pty.openpty()
    try:
        with os.fdopen(slave, "r", closefd=False) as stream:
            monkeypatch.setattr(sys, "stdin", stream)
            assert vault.at_terminal() is True
    finally:
        os.close(master)
        os.close(slave)


def test_secret_set_stores_without_echo(boxes, monkeypatch, capsys):
    prompts = terminal(monkeypatch, " abcd efgh ", " abcd efgh ")
    assert main(["secret-set", "qq"]) == 0
    assert secret_path(boxes.state, "qq").read_bytes() == b"abcd efgh"
    out = capsys.readouterr()
    assert json.loads(out.out) == {"stored": "qq"}
    assert "abcd" not in out.out + out.err
    assert len(prompts) == 2 and "again" in prompts[1].lower()
    assert all("abcd" not in p for p in prompts)


def test_secret_set_replaces_the_old_secret(boxes, monkeypatch):
    terminal(monkeypatch, "first", "first")
    assert main(["secret-set", "qq"]) == 0
    terminal(monkeypatch, "second", "second")
    assert main(["secret-set", "qq"]) == 0
    assert secret_path(boxes.state, "qq").read_bytes() == b"second"
    assert sorted(p.name for p in secret_path(boxes.state, "qq").parent.iterdir()) == ["qq"]


def test_mismatch_and_empty_refused(boxes, monkeypatch, capsys):
    terminal(monkeypatch, "abcd efgh", "abcd efgX")
    assert main(["secret-set", "qq"]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "differ" in out.err and "abcd" not in out.err
    assert not exists(boxes.state, "qq")

    terminal(monkeypatch, "   ", "   ")
    assert main(["secret-set", "qq"]) == 1
    assert "empty" in capsys.readouterr().err
    assert not exists(boxes.state, "qq")

    terminal(monkeypatch, "", "")
    assert main(["secret-set", "qq"]) == 1
    assert not exists(boxes.state, "qq")


def test_a_failed_set_keeps_the_old_secret(boxes, monkeypatch):
    store(boxes.state, "qq", "kept")
    terminal(monkeypatch, "new one", "new two")
    assert main(["secret-set", "qq"]) == 1
    assert secret_path(boxes.state, "qq").read_bytes() == b"kept"


@pytest.mark.parametrize("fail", [EOFError, KeyboardInterrupt])
def test_an_interrupted_entry_stores_nothing(boxes, monkeypatch, capsys, fail):
    monkeypatch.setattr(vault, "at_terminal", lambda: True)

    def interrupted(prompt=""):
        raise fail

    monkeypatch.setattr(getpass, "getpass", interrupted)
    assert main(["secret-set", "qq"]) == 1
    assert "nothing stored" in capsys.readouterr().err
    assert not exists(boxes.state, "qq")


def test_unknown_mailbox_refused(boxes, monkeypatch, capsys):
    terminal(monkeypatch, "abcd", "abcd")
    assert main(["secret-set", "nope"]) == 1
    assert "no mailbox nope; known: qq, work, outlook" in capsys.readouterr().err
    assert not (boxes.state / "secrets").exists()


def test_an_oauth_mailbox_has_no_password(boxes, monkeypatch, capsys):
    terminal(monkeypatch, "abcd", "abcd")
    assert main(["secret-set", "outlook"]) == 1
    assert "oauth-login outlook" in capsys.readouterr().err
    assert not exists(boxes.state, "outlook")


# --- the files ---


def test_store_strips_and_leaves_no_newline(hub):
    store(hub.state, "qq", "\t abcd efgh \r\n")
    assert secret_path(hub.state, "qq") == hub.state / "secrets" / "qq"
    assert secret_path(hub.state, "qq").read_bytes() == b"abcd efgh"
    assert exists(hub.state, "qq")


def test_store_keeps_non_ascii_as_utf8(hub):
    store(hub.state, "qq", "pässwörd密码")
    assert secret_path(hub.state, "qq").read_bytes() == "pässwörd密码".encode()


@pytest.mark.parametrize("value", ["", "   ", "\r\n\t "])
def test_store_refuses_an_empty_secret(hub, value):
    with pytest.raises(MailError, match="empty"):
        store(hub.state, "qq", value)
    assert not exists(hub.state, "qq")


def test_store_refuses_a_line_break_inside(hub):
    # A password command is read up to its first line, so a second line would be lost silently.
    with pytest.raises(MailError) as caught:
        store(hub.state, "qq", "abcd\nefgh")
    assert "abcd" not in str(caught.value) and "efgh" not in str(caught.value)
    assert not exists(hub.state, "qq")


@pytest.mark.parametrize("name", ["", "../x", "a/b", "A", "x\\y", ".", ".."])
def test_a_mailbox_name_is_never_a_path(hub, name):
    for call in (secret_path, exists, remove):
        with pytest.raises(MailError, match="mailbox name"):
            call(hub.state, name)
    with pytest.raises(MailError, match="mailbox name"):
        store(hub.state, name, "abcd")


def test_remove_says_whether_there_was_one(hub):
    assert remove(hub.state, "qq") is False
    store(hub.state, "qq", "abcd")
    assert remove(hub.state, "qq") is True
    assert not exists(hub.state, "qq")
    assert remove(hub.state, "qq") is False


@pytest.mark.skipif(not POSIX, reason="permission bits mean something only on POSIX")
def test_secret_file_is_0600(hub):
    store(hub.state, "qq", "abcd")
    assert mode(secret_path(hub.state, "qq")) == 0o600
    assert mode(hub.state / "secrets") == 0o700
    store(hub.state, "qq", "efgh")  # replaced: still private
    assert mode(secret_path(hub.state, "qq")) == 0o600


# --- the command that fetches a secret ---


def test_fetch_command_is_argv_calling_secret_fetch(boxes, make_exe):
    make_exe("uv", "")
    ctx = load(boxes.root)
    argv = fetch_command(ctx, "qq")
    uv = shutil.which("uv")
    assert uv is not None
    assert argv == [uv, "run", str(APP_DIR / "scripts" / "mx.py"), "--hub",
                    str(boxes.root.resolve()), "secret-fetch", "qq"]
    assert Path(argv[0]).is_absolute() and Path(argv[2]).is_absolute()
    assert Path(argv[4]).is_absolute()
    assert Path(argv[2]).is_file()
    assert mx_command(ctx) == argv[:5]
    assert mx_command(ctx, "check", "--all") == [*argv[:5], "check", "--all"]


def test_mx_command_makes_a_relative_uv_absolute(boxes, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: os.path.join("rel", "uv"))
    argv = mx_command(load(boxes.root), "x")
    assert Path(argv[0]).is_absolute()
    assert Path(argv[0]) == Path.cwd() / "rel" / "uv"


def test_mx_command_needs_uv(boxes, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(MailError, match="uv"):
        mx_command(load(boxes.root), "secret-fetch", "qq")


def test_secret_fetch_prints_exactly_the_secret(boxes, capsys):
    store(boxes.state, "qq", "abcd efgh")
    assert main(["secret-fetch", "qq"]) == 0
    out = capsys.readouterr()
    assert out.out == "abcd efgh"
    assert out.err == ""


def test_secret_fetch_without_a_secret_says_how(boxes, capsys):
    assert main(["secret-fetch", "qq"]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == (
        "mx: no password stored for qq: type it in the server's terminal "
        "with mx secret-set qq\n")


def test_secret_fetch_of_an_unknown_or_oauth_mailbox(boxes, capsys):
    assert main(["secret-fetch", "nope"]) == 1
    assert "no mailbox nope" in capsys.readouterr().err
    assert main(["secret-fetch", "outlook"]) == 1
    assert "oauth-login outlook" in capsys.readouterr().err


def test_secret_fetch_does_not_leak_through_a_bad_file(boxes, capsys):
    secret_path(boxes.state, "qq").parent.mkdir(parents=True)
    secret_path(boxes.state, "qq").write_bytes(b"\xffhunter2")  # not UTF-8
    assert main(["secret-fetch", "qq"]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "not UTF-8" in out.err
    assert "hunter2" not in out.err and "xff" not in out.err


def test_secret_fetch_is_not_in_the_help():
    text = cli.build_parser().format_help()
    assert "secret-set" in text and "secret-status" in text
    assert "secret-fetch" not in text and "SUPPRESS" not in text


def test_the_real_command_prints_the_bytes_and_nothing_else(boxes):
    """What himalaya sees: the process run from the argv, with stdin closed."""
    store(boxes.state, "qq", "pässwörd密码")
    argv = [sys.executable, str(ROOT / "mail" / "scripts" / "mx.py"), "--hub", str(boxes.root),
            "secret-fetch", "qq"]
    env = {**os.environ, "CC_HUB_MAIL_STATE": str(boxes.state)}
    done = subprocess.run(
        argv, check=False, stdin=subprocess.DEVNULL, capture_output=True, env=env,
        creationflags=NO_WINDOW, timeout=60)
    assert done.returncode == 0
    assert done.stdout == "pässwörd密码".encode()
    assert done.stderr == b""


# --- status, delete, forget ---


def test_secret_status_per_mailbox(boxes, capsys):
    store(boxes.state, "qq", "abcd")
    assert main(["secret-status"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {"qq": True, "work": False, "outlook": "oauth"}
    assert list(json.loads(out)) == ["qq", "work", "outlook"]  # file order
    assert "abcd" not in out


def test_secret_delete(boxes, capsys):
    store(boxes.state, "qq", "abcd")
    assert main(["secret-delete", "qq"]) == 0
    assert json.loads(capsys.readouterr().out) == {"deleted": "qq", "existed": True}
    assert not exists(boxes.state, "qq")
    assert main(["secret-delete", "qq"]) == 0
    assert json.loads(capsys.readouterr().out) == {"deleted": "qq", "existed": False}
    assert main(["secret-delete", "nope"]) == 1
    assert "no mailbox nope" in capsys.readouterr().err


def test_secret_delete_drops_the_token_of_an_oauth_mailbox(boxes, capsys):
    vault.store_token(boxes.state, "outlook", b'{"refresh_token": "R3FRESH"}')
    vault.store_token(boxes.state, "qq", b"{}")  # another mailbox's token stays
    store(boxes.state, "qq", "abcd")
    token = vault.token_path(boxes.state, "outlook")
    assert token.is_file()
    assert main(["secret-delete", "outlook"]) == 0
    assert json.loads(capsys.readouterr().out) == {"deleted": "outlook", "existed": True}
    assert not token.exists() and not vault.has_token(boxes.state, "outlook")
    assert vault.has_token(boxes.state, "qq") and exists(boxes.state, "qq")
    assert main(["secret-delete", "outlook"]) == 0
    assert json.loads(capsys.readouterr().out) == {"deleted": "outlook", "existed": False}


def test_remove_drops_the_password_and_the_token(hub):
    store(hub.state, "qq", "abcd")
    vault.store_token(hub.state, "qq", b"{}")
    assert remove(hub.state, "qq") is True
    assert not exists(hub.state, "qq") and not vault.has_token(hub.state, "qq")
    vault.store_token(hub.state, "qq", b"{}")  # a token alone counts as one
    assert remove(hub.state, "qq") is True
    assert remove(hub.state, "qq") is False


def fill_state(state: Path) -> None:
    store(state, "qq", "abcd")
    (state / "drafts").mkdir()
    (state / "drafts" / "d1.json").write_text("{}", "utf-8")
    (state / "himalaya.toml").write_text("x", "utf-8")
    (state / "marks.json").write_text("{}", "utf-8")


def test_forget_keeps_secrets_unless_asked(hub):
    fill_state(hub.state)
    assert forget(hub.state, secrets=False) == ["drafts", "himalaya.toml", "marks.json"]
    assert sorted(p.name for p in hub.state.iterdir()) == ["secrets"]
    assert secret_path(hub.state, "qq").read_bytes() == b"abcd"

    fill_state(hub.state)
    assert forget(hub.state, secrets=True) == ["drafts", "himalaya.toml", "marks.json", "secrets"]
    assert list(hub.state.iterdir()) == []
    assert hub.state.is_dir()  # the directory itself stays, with its mode
    assert forget(hub.state, secrets=True) == []


def test_forget_keeps_the_machine_id_unless_secrets_go_too(hub):
    # The id names this machine in the alert switch: without it, alerts that are on for this
    # machine would look meant for another one.
    fill_state(hub.state)
    machine = this_machine(hub.state)
    assert forget(hub.state, secrets=False) == ["drafts", "himalaya.toml", "marks.json"]
    assert this_machine(hub.state) == machine
    assert forget(hub.state, secrets=True) == ["machine-id", "secrets"]
    assert list(hub.state.iterdir()) == []


def test_forget_leaves_the_lock_alone(hub):
    hub.state.mkdir()
    (hub.state / "lock").write_text("held", "utf-8")
    (hub.state / "marks.json").write_text("{}", "utf-8")
    assert forget(hub.state, secrets=True) == ["marks.json"]
    assert (hub.state / "lock").is_file()


def test_forget_does_not_follow_a_link(hub, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", "utf-8")
    hub.state.mkdir()
    try:
        (hub.state / "attachments").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    assert forget(hub.state, secrets=False) == ["attachments"]
    assert (outside / "keep.txt").read_text("utf-8") == "keep"


def test_forget_command(boxes, capsys):
    fill_state(boxes.state)
    assert main(["forget"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "removed": ["drafts", "himalaya.toml", "marks.json"], "secrets_kept": True}
    assert exists(boxes.state, "qq")
    assert main(["forget", "--secrets"]) == 0
    assert json.loads(capsys.readouterr().out) == {"removed": ["secrets"], "secrets_kept": False}
    assert not exists(boxes.state, "qq")
    assert main(["forget"]) == 0
    assert json.loads(capsys.readouterr().out) == {"removed": [], "secrets_kept": False}

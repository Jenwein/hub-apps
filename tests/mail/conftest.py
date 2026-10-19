"""Fixtures of the mail tests: a hub with the seed mail directory, and fake executables.

Helpers that tests import by name are in mailtest.py: nothing imports from conftest.
"""

import imaplib
import json
import os
import shutil
import subprocess
import sys
import uuid
from contextlib import suppress
from pathlib import Path

import pytest

# Not left to pytest's import mode: mailtest, and later fakes, must import in any of them.
sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeHimalaya, himalaya_body
from mailtest import (
    GREENMAIL_BOX,
    GREENMAIL_SECRET,
    ROOT,
    SEED,
    Hub,
    SmtpProxy,
    windows_launcher,
    write_mailboxes,
    write_placeholder_config,
)

sys.path.insert(0, str(ROOT / "mail" / "scripts"))

from mailkit import vault
from mailkit.cli import main
from mailkit.context import load


@pytest.fixture
def hub(tmp_path, monkeypatch) -> Hub:
    root = tmp_path / "hub"
    root.mkdir()
    data = root / "mail"
    shutil.copytree(SEED, data)
    state = tmp_path / "state"
    monkeypatch.setenv("CC_HUB_MAIL_STATE", str(state))
    monkeypatch.chdir(root)
    return Hub(root, data, state)


@pytest.fixture
def make_exe(tmp_path, monkeypatch):
    """Return make_exe(name, body) -> Path: an executable `name` that runs the Python `body`.

    The script `<tmp>/bin/<name>.py` runs through sys.executable behind a shell script (POSIX) or
    an `.exe` launcher (Windows), and gets its arguments, stdin, stdout, stderr and exit status
    unchanged. Not a `.cmd` shim: cmd.exe reads `&`, `%` and `^` in the arguments (every non-ASCII
    IMAP folder name has an `&`). The directory `<tmp>/bin` is first on PATH.
    """
    folder = tmp_path / "bin"
    folder.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ.get('PATH', '')}")

    def make(name: str, body: str) -> Path:
        script = folder / f"{name}.py"
        script.write_text(body, "utf-8", newline="\n")
        if sys.platform == "win32":
            return windows_launcher(folder, tmp_path / "stubs", name, script)
        exe = folder / name
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', "utf-8")
        exe.chmod(0o755)
        return exe

    return make


@pytest.fixture
def fake_himalaya(hub, tmp_path, make_exe):
    """Return make(server) -> FakeHimalaya: a fake `himalaya` first on PATH that answers from the
    `server` dict (the format is described at FAKE_HIMALAYA in fakes.py) and logs its calls. The
    state directory gets a placeholder himalaya.toml (Himalaya.run wants the file; the fake does
    not read it).

    One fake per test. The test fails at teardown if a call exited 99 (the wrapper asked for
    something F-NODEL forbids) or 97 (the fake crashed).
    """
    made: list[FakeHimalaya] = []

    def make(server: dict) -> FakeHimalaya:
        if made:
            raise RuntimeError("one fake himalaya per test")
        write_placeholder_config(hub)
        folder = tmp_path / "fake-himalaya"
        folder.mkdir()
        path, log = folder / "server.json", folder / "calls.jsonl"
        fake = FakeHimalaya(make_exe("himalaya", himalaya_body(path, log)), path, log)
        fake.write(server)
        made.append(fake)
        return fake

    yield make
    calls = [call for fake in made for call in fake.calls()]
    forbidden = [call["argv"][1:] for call in calls if call["exit"] == 99]
    if forbidden:
        pytest.fail(f"the fake himalaya was asked for what F-NODEL forbids: {forbidden}")
    crashed = [call["argv"][1:] for call in calls if call["exit"] == 97]
    if crashed:
        pytest.fail(f"the fake himalaya crashed on: {crashed}")


GREENMAIL_FOLDERS = ("Sent", "Trash", "Archive")  # INBOX is there for every user


@pytest.fixture
def greenmail_hub(hub, capsys, request) -> Hub:
    """The hub, set up against a GreenMail server: skips unless MAIL_TEST_IMAP=host:port is set.

    A fresh mailbox `test` (see GREENMAIL_BOX): provider gmail, the address `test-<uuid>@localhost`,
    IMAP at MAIL_TEST_IMAP and SMTP at MAIL_TEST_SMTP (default: the IMAP host, port 3025), both
    without TLS, `save_copy`, the password sent by `login` (GreenMail refuses AUTHENTICATE PLAIN),
    SMTP through an SmtpProxy (himalaya cannot read the server name GreenMail announces),
    and the folders INBOX, Sent, Trash and Archive (made over IMAP). The server takes any password
    (authentication is off) and makes the user on first login. The password `x` is stored as a
    secret, `mx configure` has written the himalaya config, and the password command of that
    config has run once, which also fills uv's cache for the script (the first call of himalaya
    would otherwise spend its timeout on it).
    """
    target = os.environ.get("MAIL_TEST_IMAP")
    if not target:
        pytest.skip("MAIL_TEST_IMAP=host:port is not set")
    imap_host, _, imap_port = target.rpartition(":")
    smtp = os.environ.get("MAIL_TEST_SMTP") or f"{imap_host}:3025"
    smtp_host, _, smtp_port = smtp.rpartition(":")
    proxy = SmtpProxy((smtp_host, int(smtp_port)))
    request.addfinalizer(proxy.stop)
    address = f"test-{uuid.uuid4().hex}@localhost"
    write_mailboxes(hub, f"""[[mailbox]]
name = "{GREENMAIL_BOX}"
address = "{address}"
provider = "gmail"
imap_host = "{imap_host}"
imap_port = {int(imap_port)}
imap_tls = "none"
smtp_host = "127.0.0.1"
smtp_port = {proxy.port}
smtp_tls = "none"
save_copy = true
sasl = "login"

[mailbox.folders]
inbox = "INBOX"
sent = "Sent"
trash = "Trash"
archive = "Archive"
""")
    conn = imaplib.IMAP4(imap_host, int(imap_port), timeout=30)
    try:
        conn.login(address, GREENMAIL_SECRET)
        for folder in GREENMAIL_FOLDERS:
            answer, data = conn.create(folder)
            assert answer == "OK", f"GreenMail did not create {folder}: {data}"
    finally:
        with suppress(imaplib.IMAP4.error, OSError):
            conn.logout()
    vault.store(hub.state, GREENMAIL_BOX, GREENMAIL_SECRET)
    code = main(["--hub", str(hub.root), "configure"])
    done = capsys.readouterr()
    assert code == 0, done.err
    assert "himalaya.toml" in json.loads(done.out)["written"]
    ctx = load(hub.root)
    fetched = subprocess.run(
        vault.fetch_command(ctx, GREENMAIL_BOX), check=False, cwd=hub.state,
        stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
    assert fetched.stdout == GREENMAIL_SECRET.encode(), fetched.stderr.decode("utf-8", "replace")
    return hub

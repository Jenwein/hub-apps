"""Fixtures of the mail tests: a hub with the seed mail directory, and fake executables.

Helpers that tests import by name are in mailtest.py: nothing imports from conftest.
"""

import os
import shutil
import sys
from pathlib import Path

import pytest

# Not left to pytest's import mode: mailtest, and later fakes, must import in any of them.
sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeHimalaya, himalaya_body
from mailtest import ROOT, SEED, Hub, windows_launcher, write_placeholder_config

sys.path.insert(0, str(ROOT / "mail" / "scripts"))


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

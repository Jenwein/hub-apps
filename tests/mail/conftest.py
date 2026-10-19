"""Fixtures of the mail tests: a hub with the seed mail directory, and fake executables."""

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SEED = ROOT / "mail" / "seed" / "mail"
sys.path.insert(0, str(ROOT / "mail" / "scripts"))


@dataclass(frozen=True)
class Hub:
    root: Path  # the hub; the working directory of the test
    data: Path  # root/mail, a copy of the seed mail directory
    state: Path  # the per-machine state directory (created by the first use)


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


def write_mailboxes(hub: Hub, text: str) -> None:
    (hub.data / "mailboxes.toml").write_text(text, "utf-8", newline="\n")


@pytest.fixture
def make_exe(tmp_path, monkeypatch):
    """Return make_exe(name, body) -> Path: an executable `name` that runs the Python `body`.

    The script runs through sys.executable behind a shell script (POSIX) or a .cmd shim
    (Windows). Its directory, `<tmp>/bin`, is first on PATH.
    """
    folder = tmp_path / "bin"
    folder.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ.get('PATH', '')}")

    def make(name: str, body: str) -> Path:
        script = folder / f"{name}.py"
        script.write_text(body, "utf-8", newline="\n")
        if sys.platform == "win32":
            exe = folder / f"{name}.cmd"
            exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n', "utf-8", newline="")
        else:
            exe = folder / name
            exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', "utf-8")
            exe.chmod(0o755)
        return exe

    return make

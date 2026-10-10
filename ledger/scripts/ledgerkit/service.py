"""Fava as a background service: a systemd user unit on Linux, a launchd agent on macOS.

The text is printed, not written: the setup step decides where it goes.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

from .settings import LedgerError

UNIT_NAME = "cc-hub-ledger-fava.service"
LAUNCHD_LABEL = "io.cc-hub.ledger.fava"
HOST = "127.0.0.1"

# systemd misreads these inside a quoted ExecStart word (quote, escape, specifier, line end).
UNSAFE_IN_UNIT = ('"', "\\", "%", "\n", "\r")


def _quoted(text: str, what: str) -> str:
    if any(char in text for char in UNSAFE_IN_UNIT):
        raise LedgerError(f"{what} cannot contain a double quote, backslash, percent sign "
                          f"or line break: {text!r}")
    return f'"{text}"'


def fava_unit(platform: str, fava: str, ledger: Path, port: int) -> str:
    """The service definition that runs fava on `ledger` (its main.beancount) at `port`."""
    if platform == "linux":
        exec_start = " ".join([
            _quoted(fava, "the fava path"),
            "-H", HOST, "-p", str(port),
            _quoted(ledger.as_posix(), "the ledger path"),
        ])
        return "\n".join([
            "[Unit]",
            "Description=Fava for the cc-hub ledger",
            "",
            "[Service]",
            f"ExecStart={exec_start}",
            "Restart=on-failure",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ])
    if platform == "macos":
        return plistlib.dumps({
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": [fava, "-H", HOST, "-p", str(port), ledger.as_posix()],
            "RunAtLoad": True,
            "KeepAlive": True,
        }).decode("utf-8")
    raise LedgerError(f"Fava runs on demand on {platform}; see fava.md")

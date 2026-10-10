import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SEED = ROOT / "ledger" / "seed" / "ledger"
sys.path.insert(0, str(ROOT / "ledger" / "scripts"))

# marker -> (executable it needs, reason shown for the skip)
NEEDS_EXECUTABLE = {
    "deb": "double-entry-generator",
    "cchub": "cc-hub",
}


def pytest_collection_modifyitems(config, items):
    for item in items:
        for marker, exe in NEEDS_EXECUTABLE.items():
            if marker in item.keywords and shutil.which(exe) is None:
                item.add_marker(pytest.mark.skip(reason=f"{exe} is not on PATH"))


@pytest.fixture
def make_ledger(tmp_path):
    """Return make_ledger(accounts="", txns=None): a fresh copy of the seed ledger.

    `accounts` is appended to accounts.beancount; `txns` maps a year to the text of
    txns/<year>.beancount. Returns the ledger directory.
    """

    def make(accounts: str = "", txns: dict[int, str] | None = None) -> Path:
        ledger = tmp_path / "ledger"
        shutil.copytree(SEED, ledger)
        if accounts:
            path = ledger / "accounts.beancount"
            text = path.read_text("utf-8") + accounts
            if not text.endswith("\n"):
                text += "\n"
            path.write_text(text, "utf-8", newline="\n")
        for year, text in (txns or {}).items():
            (ledger / "txns" / f"{year}.beancount").write_text(text, "utf-8", newline="\n")
        return ledger

    return make


# What a fake double-entry-generator writes to -o: one transaction, the way the real one does.
FAKE_ONE_TXN = """\
option "title" "ledger"
option "operating_currency" "CNY"

1970-01-01 open Assets:Alipay:Balance
1970-01-01 open Expenses:FIXME

2026-09-03 * "饭店" "午餐"
\torderId: "O1"
\tExpenses:FIXME 20.00 CNY
\tAssets:Alipay:Balance -20.00 CNY
"""

# Only the header and the account list: the file the real converter writes for an unknown export.
FAKE_NO_TXN = """\
option "title" "ledger"
option "operating_currency" "CNY"

1970-01-01 open Assets:Alipay:Balance
"""

_FAKE_BODY = '''\
import hashlib, json, sys


def run(log, output, fail):
    argv = sys.argv[1:]
    config = argv[argv.index("--config") + 1] if "--config" in argv else None
    with open(log, "w", encoding="utf-8") as stream:
        json.dump({
            "argv": argv,
            "sha256": hashlib.sha256(open(argv[-1], "rb").read()).hexdigest(),
            "config": open(config, encoding="utf-8").read() if config else None,
        }, stream, ensure_ascii=False)
    if fail:
        print(fail, file=sys.stderr)
        sys.exit(1)
    with open(argv[argv.index("-o") + 1], "wb") as stream:
        stream.write(output.encode("utf-8"))
'''


@pytest.fixture
def make_fake_deb(tmp_path):
    """Return make(output=..., fail=None, name="fake-deb") -> (executable, log path).

    The executable stands in for double-entry-generator: a Python script run through
    sys.executable, behind a shell script (POSIX) or a .cmd shim (Windows). It records its
    arguments, the sha256 of the bill (the last argument) and the config it was given as JSON
    in the log, then writes `output` to the file after -o. With `fail` it prints that to stderr
    and exits with 1 instead. Its directory is `<tmp>/bin`.
    """

    def make(output: str = FAKE_ONE_TXN, *, fail: str | None = None, name: str = "fake-deb"):
        folder = tmp_path / "bin"
        folder.mkdir(exist_ok=True)
        log = tmp_path / f"{name}.log.json"
        script = folder / f"{name}.py"
        script.write_text(
            f"{_FAKE_BODY}\nrun({str(log)!r}, {output!r}, {fail!r})\n", "utf-8", newline="\n")
        if sys.platform == "win32":
            exe = folder / f"{name}.cmd"
            exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n', "utf-8", newline="")
        else:
            exe = folder / name
            exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', "utf-8")
            exe.chmod(0o755)
        return exe, log

    return make


@pytest.fixture
def fake_deb(make_fake_deb):
    return make_fake_deb()


@pytest.fixture
def fake_deb_empty(make_fake_deb):
    return make_fake_deb(FAKE_NO_TXN)

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

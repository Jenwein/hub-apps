"""Helpers the mail tests import by name (the fixtures themselves are in conftest.py)."""

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).parents[2]
SEED = ROOT / "mail" / "seed" / "mail"


@dataclass(frozen=True)
class Hub:
    root: Path  # the hub; the working directory of the test
    data: Path  # root/mail, a copy of the seed mail directory
    state: Path  # the per-machine state directory (created by the first use)


def write_mailboxes(hub: Hub, text: str) -> None:
    (hub.data / "mailboxes.toml").write_text(text, "utf-8", newline="\n")

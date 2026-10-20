"""What a command works on: the hub, the app, its data and its per-machine state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .paths import state_dir
from .settings import Mailbox, MailError, Settings, load_mailboxes, load_settings

# The app directory: <app>/scripts/mailkit/context.py, three levels below it.
APP_DIR = Path(__file__).resolve().parents[2]


@dataclass
class Ctx:
    hub: Path
    app: Path  # the app's own files: skills, rules, report sections
    data: Path  # <hub>/mail: mailboxes.toml, config.toml, rules.md
    state: Path  # per-machine state: secrets, generated configs, drafts, marks
    settings: Settings
    mailboxes: dict[str, Mailbox]

    def mailbox(self, name: str) -> Mailbox:
        found = self.mailboxes.get(name)
        if found is None:
            known = ", ".join(self.mailboxes) or "none"
            raise MailError(f"no mailbox {name}; known: {known}")
        return found


def load(hub: Path) -> Ctx:
    """The context of the hub at `hub`. The state directory is made after the files have parsed."""
    hub = Path(hub).resolve()
    data = hub / "mail"
    if not (data / "mailboxes.toml").is_file():
        raise MailError(f"{hub} has no mail/mailboxes.toml: run mx from the hub root")
    settings = load_settings(data)
    mailboxes = load_mailboxes(data)
    return Ctx(hub, APP_DIR, data, state_dir(hub), settings, mailboxes)

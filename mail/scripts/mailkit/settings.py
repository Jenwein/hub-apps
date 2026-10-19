"""Settings of the mail app: the mailboxes (mailboxes.toml), config.toml, and the providers."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


class MailError(Exception):
    """An expected failure. The command line prints `mx: <message>` and exits with 1."""


# A mailbox name is a file name in the state directory and an argument of every command.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")

TLS_MODES = ("tls", "starttls", "none")
FOLDER_ROLES = ("inbox", "sent", "trash", "archive")


@dataclass(frozen=True)
class Server:
    host: str
    port: int
    tls: str  # "tls" | "starttls" | "none"


@dataclass(frozen=True)
class Provider:
    name: str
    imap: Server
    smtp: Server
    auth: str  # "password" | "oauth"
    imap_id: bool  # the server wants an IMAP ID command before it lets the client in
    save_copy: bool  # the client must append a copy of sent mail to the sent folder itself


PROVIDERS: dict[str, Provider] = {
    "feishu": Provider(
        "feishu", Server("imap.feishu.cn", 993, "tls"), Server("smtp.feishu.cn", 465, "tls"),
        "password", False, True),
    "qq": Provider(
        "qq", Server("imap.qq.com", 993, "tls"), Server("smtp.qq.com", 465, "tls"),
        "password", True, True),
    "gmail": Provider(
        "gmail", Server("imap.gmail.com", 993, "tls"), Server("smtp.gmail.com", 465, "tls"),
        "password", False, False),
    "outlook": Provider(
        "outlook", Server("outlook.office365.com", 993, "tls"),
        Server("smtp-mail.outlook.com", 587, "starttls"), "oauth", False, True),
}


@dataclass(frozen=True)
class Mailbox:
    name: str
    address: str
    display_name: str
    provider: str
    imap: Server
    smtp: Server
    auth: str
    imap_id: bool
    save_copy: bool
    folders: dict[str, str]  # role -> the server's folder name; "" = not known yet


@dataclass(frozen=True)
class Settings:
    alerts_enabled: bool
    alerts_host: str
    reader_model: str
    language: str


def _read(path: Path) -> dict:
    if not path.is_file():
        raise MailError(f"no {path.name} in {path.parent}")
    try:
        return tomllib.loads(path.read_text("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as err:
        raise MailError(f"cannot read {path}: {err}") from err


# `where` names the key in the message, such as `config.toml: alerts.enabled`.


def _table(parent: dict, key: str, where: str) -> dict:
    value = parent.get(key, {})
    if not isinstance(value, dict):
        raise MailError(f"{where} must be a table")
    return value


def _str(table: dict, key: str, default: str, where: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise MailError(f"{where} must be a string")
    return value


def _int(table: dict, key: str, default: int, where: str) -> int:
    value = table.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise MailError(f"{where} must be an integer")
    return value


def _bool(table: dict, key: str, default: bool, where: str) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise MailError(f"{where} must be true or false")
    return value


def _server(entry: dict, prefix: str, base: Server, where: str) -> Server:
    """The provider's server, with whichever of `<prefix>_host`, `_port` and `_tls` the entry sets."""
    host = _str(entry, f"{prefix}_host", base.host, f"{where}: {prefix}_host")
    port = _int(entry, f"{prefix}_port", base.port, f"{where}: {prefix}_port")
    tls = _str(entry, f"{prefix}_tls", base.tls, f"{where}: {prefix}_tls")
    if not 0 < port < 65536:
        raise MailError(f"{where}: {prefix}_port must be between 1 and 65535, got {port}")
    if tls not in TLS_MODES:
        raise MailError(f"{where}: {prefix}_tls must be one of {', '.join(TLS_MODES)}, got {tls!r}")
    return Server(host, port, tls)


def _mailbox(entry: dict, index: int) -> Mailbox:
    name = entry.get("name")
    if not isinstance(name, str):
        raise MailError(f"mailboxes.toml: mailbox #{index}: name is missing or not a string")
    if not NAME_RE.fullmatch(name):  # fullmatch: `$` alone lets a trailing newline through
        raise MailError(
            f"mailboxes.toml: mailbox #{index}: name {name!r} must be 1 to 31 lowercase letters, "
            "digits and dashes, starting with a letter or digit")
    where = f'mailboxes.toml: mailbox "{name}"'
    address = _str(entry, "address", "", f"{where}: address")
    if "@" not in address:
        raise MailError(f"{where}: address {address!r} needs an @")
    provider_name = _str(entry, "provider", "", f"{where}: provider")
    provider = PROVIDERS.get(provider_name)
    if provider is None:
        raise MailError(
            f"{where}: provider {provider_name!r} is not one of {', '.join(PROVIDERS)}")
    folders = {"inbox": "INBOX", "sent": "", "trash": "", "archive": ""}
    given = _table(entry, "folders", f"{where}: folders")
    for role in FOLDER_ROLES:
        folders[role] = _str(given, role, folders[role], f"{where}: folders.{role}")
    return Mailbox(
        name=name,
        address=address,
        display_name=_str(entry, "display_name", "", f"{where}: display_name"),
        provider=provider.name,
        imap=_server(entry, "imap", provider.imap, where),
        smtp=_server(entry, "smtp", provider.smtp, where),
        auth=provider.auth,
        imap_id=provider.imap_id,
        save_copy=_bool(entry, "save_copy", provider.save_copy, f"{where}: save_copy"),
        folders=folders,
    )


def load_mailboxes(data_dir: Path) -> dict[str, Mailbox]:
    """The mailboxes of mailboxes.toml in file order. A bad entry is a MailError naming it."""
    data = _read(Path(data_dir) / "mailboxes.toml")
    entries = data.get("mailbox", [])
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise MailError("mailboxes.toml: mailbox must be a list of [[mailbox]] tables")
    found: dict[str, Mailbox] = {}
    for index, entry in enumerate(entries, 1):
        box = _mailbox(entry, index)
        if box.name in found:
            raise MailError(f'mailboxes.toml: mailbox "{box.name}" is defined twice')
        found[box.name] = box
    return found


def load_settings(data_dir: Path) -> Settings:
    """Settings from config.toml. Missing keys take the seed values."""
    data = _read(Path(data_dir) / "config.toml")
    alerts = _table(data, "alerts", "config.toml: [alerts]")
    reader = _table(data, "reader", "config.toml: [reader]")
    chat = _table(data, "chat", "config.toml: [chat]")
    return Settings(
        alerts_enabled=_bool(alerts, "enabled", False, "config.toml: alerts.enabled"),
        alerts_host=_str(alerts, "host", "", "config.toml: alerts.host"),
        reader_model=_str(reader, "model", "sonnet", "config.toml: reader.model"),
        language=_str(chat, "language", "", "config.toml: chat.language"),
    )

"""The configs of himalaya, carillon and ortie, generated from mailboxes.toml.

The agent never edits them: `configure` writes `himalaya.toml`, `carillon.toml` and, with an
Outlook mailbox, `ortie.toml` into the state directory. They hold the command that fetches a
secret, never the secret itself. The tools always run in the state directory and name their config
by a relative file name, because they split `-c` at colons and an absolute `C:\\...` would break.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import tomli_w

from .context import Ctx
from .paths import read_json, write_json, write_private
from .settings import FOLDER_ROLES, Mailbox, MailError, Server
from .vault import fetch_command, mx_command

HIMALAYA_CONFIG = "himalaya.toml"
CARILLON_CONFIG = "carillon.toml"
ORTIE_CONFIG = "ortie.toml"
OAUTH_FILE = "oauth.json"  # the grant each Outlook mailbox signed in with

# Thunderbird's public Microsoft client, which ortie's README offers: ortie has none of its own.
OUTLOOK_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
_LOGIN = "https://login.microsoftonline.com/common/oauth2/v2.0"
OUTLOOK_SCOPES = [
    "https://outlook.office.com/IMAP.AccessAsUser.All",
    "https://outlook.office.com/SMTP.Send",
    "offline_access",
]
GRANTS = ("device", "authorization-code")

# What QQ's IMAP server wants to know before it lets a client in.
IMAP_ID_FIELDS = {"name": True, "version": True, "vendor": True, "support-url": True}


def server_url(scheme: str, server: Server) -> tuple[str, bool]:
    """The server URL of himalaya and carillon, and whether the connection upgrades with STARTTLS.
    `scheme` is `imap` or `smtp`: TLS from the start is `imaps` or `smtps`."""
    if server.tls == "tls":
        return f"{scheme}s://{server.host}:{server.port}", False
    return f"{scheme}://{server.host}:{server.port}", server.tls == "starttls"


def ortie_executable() -> str:
    """The absolute path of ortie, or the bare name while it is not installed yet, so the configs
    can be written (and tested) before `mx install-tool ortie`."""
    found = shutil.which("ortie")
    if found is None:
        return "ortie"
    return found if os.path.isabs(found) else os.path.abspath(found)


def ortie_argv(executable: str, mailbox: str, *args: str) -> list[str]:
    """An ortie call for `mailbox`: its config by the relative name, so it runs in the state
    directory."""
    return [executable, "-c", ORTIE_CONFIG, "-a", mailbox, *args]


def oauth_token_command(ctx: Ctx, mailbox: str) -> list[str]:
    """How himalaya and carillon get a fresh access token."""
    return ortie_argv(ortie_executable(), mailbox, "token", "show", "--auto-refresh")


def hook_command(ctx: Ctx, mailbox: str) -> list[str]:
    """What carillon runs for each new mail of `mailbox`. It gets no account name, so the
    mailbox is in the argv."""
    return mx_command(ctx, "hook", mailbox)


def credentials(ctx: Ctx, mb: Mailbox) -> dict:
    """The `sasl` table: a password or a token is fetched by a command, never written here."""
    if mb.auth == "oauth":
        return {"xoauth2": {
            "username": mb.address, "token": {"command": oauth_token_command(ctx, mb.name)}}}
    return {"plain": {
        "username": mb.address, "password": {"command": fetch_command(ctx, mb.name)}}}


def _backend(ctx: Ctx, mb: Mailbox, scheme: str, server: Server, *, send_id: bool = False) -> dict:
    """The `imap` or `smtp` table of an account (the tables reject keys they do not know)."""
    url, starttls = server_url(scheme, server)
    table: dict = {"server": url}
    if starttls:
        table["starttls"] = True
    table["sasl"] = credentials(ctx, mb)
    if send_id:
        table["id"] = {"auto": True, "fields": dict(IMAP_ID_FIELDS)}
    return table


def himalaya_config(ctx: Ctx) -> dict:
    accounts: dict = {}
    for index, mb in enumerate(ctx.mailboxes.values()):
        account: dict = {}
        if index == 0:
            account["default"] = True
        account["email"] = mb.address
        if mb.display_name:
            account["display-name"] = mb.display_name
        # IMAP gives only INBOX a role, so every other role needs an alias; a role whose folder
        # is not known yet has none. Sent copies are appended by mx, not by himalaya.
        aliases = {role: mb.folders[role] for role in FOLDER_ROLES if mb.folders.get(role)}
        if aliases:
            account["mailbox"] = {"alias": aliases}
        account["imap"] = _backend(ctx, mb, "imap", mb.imap, send_id=mb.imap_id)
        account["smtp"] = _backend(ctx, mb, "smtp", mb.smtp)
        accounts[mb.name] = account
    return {"accounts": accounts}


def carillon_config(ctx: Ctx) -> dict:
    """Each mailbox's inbox is watched, and every new mail runs the `hook` command of mx."""
    accounts: dict = {}
    for mb in ctx.mailboxes.values():
        imap = {"mailbox": mb.folders.get("inbox") or "INBOX"}
        imap.update(_backend(ctx, mb, "imap", mb.imap, send_id=mb.imap_id))
        imap["hook"] = {"on-message-added": {"cmd": hook_command(ctx, mb.name)}}
        accounts[mb.name] = {"imap": imap}
    return {"accounts": accounts}


def grant_of(state: Path, mailbox: str) -> str:
    """The grant `mailbox` signs in with: the device grant, unless `oauth-login --paste` chose the
    authorization code."""
    data = read_json(Path(state) / OAUTH_FILE, {})
    grant = data.get(mailbox) if isinstance(data, dict) else None
    return grant if grant in GRANTS else "device"


def record_grant(state: Path, mailbox: str, grant: str) -> None:
    if grant not in GRANTS:
        raise MailError(f"unknown grant {grant!r}")
    path = Path(state) / OAUTH_FILE
    data = read_json(path, {})
    if not isinstance(data, dict):
        data = {}
    data[mailbox] = grant
    write_json(path, data)


def ortie_config(ctx: Ctx) -> dict:
    """One ortie account per OAuth mailbox. ortie keeps no token itself: it reads and writes it
    through the `token-store` command of mx."""
    accounts: dict = {}
    for mb in ctx.mailboxes.values():
        if mb.auth != "oauth":
            continue
        accounts[mb.name] = {
            "client-id": OUTLOOK_CLIENT_ID,
            "endpoints": {
                "authorization": f"{_LOGIN}/authorize",
                "token": f"{_LOGIN}/token",
                "device-authorization": f"{_LOGIN}/devicecode",
                "redirection": "https://localhost",
            },
            "scopes": list(OUTLOOK_SCOPES),
            "grant": grant_of(ctx.state, mb.name),
            "auto-refresh": True,
            "storage": {
                "read": {"command": mx_command(ctx, "token-store", "read", mb.name)},
                "write": {"command": mx_command(ctx, "token-store", "write", mb.name)},
            },
        }
    return {"accounts": accounts}


def _write_if_changed(path: Path, text: bytes) -> bool:
    try:
        if path.read_bytes() == text:
            return False
    except FileNotFoundError:
        pass
    write_private(path, text)
    return True


def write_configs(ctx: Ctx) -> list[str]:
    """Write the configs this hub needs into the state directory, private, and only those whose
    text changed (a file that is the same stays untouched). Returns the names written."""
    wanted = {
        HIMALAYA_CONFIG: himalaya_config(ctx),
        CARILLON_CONFIG: carillon_config(ctx),
    }
    if any(mb.auth == "oauth" for mb in ctx.mailboxes.values()):
        wanted[ORTIE_CONFIG] = ortie_config(ctx)
    written: list[str] = []
    for name, config in wanted.items():
        path = ctx.state / name
        try:
            if _write_if_changed(path, tomli_w.dumps(config).encode("utf-8")):
                written.append(name)
        except OSError as err:
            raise MailError(f"cannot write {path}: {err}") from err
    return written

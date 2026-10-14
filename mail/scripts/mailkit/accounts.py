"""Mailboxes: detect, add, test, edit, remove and list them, and keep their passwords.

A mailbox is three things: a `[mailboxes."<name>"]` section in config.toml (in git), its
password in `secrets/mail/<address>.toml` under the hub (git-ignored), and its state in the
local index. Adding one logs in to both of its servers before anything is written, records
where its inbox stands so that mail already there is never taken for new mail, and leaves
nothing behind when a later step fails.

A password never appears in a result, an error message or config.toml. The secret file's name
comes from the address through `address_file_name`, never the raw address: a name a file system
cannot hold or that would leave the folder is impossible.

Every function takes the hub directory (where secrets/ is) and the mail directory (where
config.toml is); `connect` is the server factory (default: `server.connect`), which tests
replace with tests/mailfakes.fake_connect.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import config, providers, server
from .config import ADDRESS, Mailbox, MailError
from .providers import PROVIDERS, Provider
from .server import LoginError, password_problem
from .store import Store

SECRETS = ("secrets", "mail")  # under the hub
GUIDES = "skills/mail/providers"  # where the provider guides are, in the app
RECENT_DAYS = 90  # mail of the last 90 days is indexed (as old mail) after a mailbox is added
NAME_LIMIT = 200  # characters of a file name made from an address
PER_PAGE = 10

_UNSAFE = re.compile(r"[^a-z0-9._+\-@]")
_DEVICES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


# -- addresses and secrets -------------------------------------------------------------------


def _ascii_lower(text: str) -> str:
    return "".join(c.lower() if "A" <= c <= "Z" else c for c in text)


def same_address(a: str, b: str) -> bool:
    """Whether two addresses are the same mailbox (ASCII letters compared without case)."""
    return _ascii_lower(a.strip()) == _ascii_lower(b.strip())


def address_file_name(address: str) -> str:
    """A file name (without extension) for `address` that every file system takes.

    ASCII letters are lowered; letters, digits and `. _ + - @` stay; any other character
    becomes `_`, and then `~` and a short hash of the address are added, so two addresses never
    share a name (an address that needs no change has no `~`). MailError for a text that would
    make an empty name, `.`, `..` or a Windows device name.
    """
    lowered = _ascii_lower(address)
    name = _UNSAFE.sub("_", lowered)
    if name != lowered or len(name) > NAME_LIMIT:
        digest = hashlib.sha256(lowered.encode("utf-8", "surrogatepass")).hexdigest()[:10]
        name = f"{name[:NAME_LIMIT]}~{digest}"
    if name in ("", ".", "..") or name.split(".", 1)[0] in _DEVICES:
        raise MailError(f"{address!r} cannot be used as a file name")
    return name


def secret_path(hub: Path, address: str) -> Path:
    """Where the password of `address` is kept: `<hub>/secrets/mail/<file name>.toml`."""
    return Path(hub).joinpath(*SECRETS, f"{address_file_name(address)}.toml")


def read_password(hub: Path, address: str) -> str:
    """The stored password of `address`; MailError when there is none or the file is broken
    (the message names the file, never its content)."""
    path = secret_path(hub, address)
    again = "store it again with: mk account edit <name> --password-stdin"
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise MailError(f"no password stored for {address} ({path} is missing); {again}") from None
    except OSError as exc:
        raise MailError(f"cannot read the password file {path}: {exc.strerror or exc}") from None
    try:
        table = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise MailError(f"the password file {path} is not valid TOML; {again}") from None
    password = table.get("password")
    if not isinstance(password, str) or not password:
        raise MailError(f'the password file {path} has no password = "..." line; {again}')
    return password


def _write_password(hub: Path, address: str, password: str) -> None:
    path = secret_path(hub, address)
    text = (
        "# The password the mail app logs in to this address with. It stays on this machine:\n"
        "# secrets/ is not in git. Removing the mailbox in chat deletes this file.\n"
        f"address = {config._quote(address)}\n"
        f"password = {config._quote(password)}\n"
    )
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        config.atomic_write(path, text.encode("utf-8"), mode=0o600)
    except OSError as exc:
        raise MailError(f"cannot write the password file {path}: {exc.strerror or exc}") from None


def _password_undo(hub: Path, address: str) -> Callable[[], None]:
    """A step that puts the password file of `address` back the way it is now: gone (with
    secrets/mail/ when this created it) or with its old content."""
    path = secret_path(hub, address)
    folder_existed = path.parent.exists()
    try:
        old = path.read_bytes()
    except FileNotFoundError:
        old = None
    except OSError as exc:
        raise MailError(f"cannot read the password file {path}: {exc.strerror or exc}") from None

    def undo() -> None:
        if old is not None:
            config.atomic_write(path, old, mode=0o600)
            return
        path.unlink(missing_ok=True)
        if not folder_existed:
            with contextlib.suppress(OSError):  # only when empty
                path.parent.rmdir()

    return undo


def _undo(steps: list[Callable[[], None]]) -> list[str]:
    """Run the undo steps, last first; the problems of those that failed."""
    problems = []
    for step in reversed(steps):
        try:
            step()
        except (MailError, OSError) as exc:
            problems.append(str(exc))
    return problems


def _rolled_back(err: BaseException, steps: list[Callable[[], None]]) -> None:
    """Undo after `err`; when undoing fails too, say what may be left."""
    problems = _undo(steps)
    if problems and isinstance(err, Exception):
        raise MailError(
            f"{err}; undoing the change failed too ({'; '.join(problems)}): check config.toml "
            "and secrets/mail/") from err


# -- results ---------------------------------------------------------------------------------


def _hint(step: str, mb: Mailbox) -> str:
    provider: Provider | None = PROVIDERS.get(mb.provider)
    name = provider.name if provider else mb.provider
    guide = f"{GUIDES}/{provider.guide}" if provider else f"the guides in {GUIDES}/"
    if step == "connect":
        return (
            f"The server did not answer. Check that this machine has network access and can "
            f"reach {mb.imap} and {mb.smtp} (a firewall or proxy may block them), then try "
            "again.")
    if step == "imap-login":
        return (
            f"{name} refused the address and password. Use the password made for mail clients "
            "(app password or authorization code), not the web sign-in password, and check "
            f"that IMAP is turned on; the steps are in {guide}.")
    return (
        f"Reading mail works, but {name} refused the same password for sending: turn on SMTP "
        f"for mail clients in the {name} settings; the steps are in {guide}.")


def _login_failed(err: LoginError, mb: Mailbox, password: str) -> dict:
    message = str(err).replace(password, "[password]") if password else str(err)
    return {"ok": False, "step": err.step, "message": message, "hint": _hint(err.step, mb)}


def _mailbox(mail_dir: Path, name: str) -> tuple[config.Settings, Mailbox]:
    settings = config.load(mail_dir)
    mb = settings.mailboxes.get(name)
    if mb is None:
        raise MailError(f'there is no mailbox named "{name}"')
    return settings, mb


def _check_password(password: str) -> None:
    problem = password_problem(password)
    if problem:
        raise MailError(problem)


def _now(now: datetime | None) -> datetime:
    now = now or datetime.now(UTC)
    return now if now.tzinfo else now.replace(tzinfo=UTC)


# -- the commands ----------------------------------------------------------------------------


def detect(address: str, *, mx=None) -> dict:
    """Which supported provider `address` belongs to, with the guide to show the user:
    `{"address", "provider", "supported", "reason"}`, plus `"name"` and `"guide"` when it is
    supported."""
    result = providers.detect(address, mx=mx or providers.lookup_mx)
    out = {"address": address.strip(), **result}
    if result["supported"]:
        provider = PROVIDERS[result["provider"]]
        out["name"] = provider.name
        out["guide"] = f"{GUIDES}/{provider.guide}"
    return out


def add(hub: Path, mail_dir: Path, name: str, address: str, password: str, *,
        provider: str | None = None, connect=None, now: datetime | None = None,
        mx=None) -> dict:
    """Add mailbox `name` for `address`.

    In this order: check the name, address and password; refuse a name or address already
    added; find the provider (`provider` names it, else the address tells it, by its domain or
    its MX records); log in to IMAP and SMTP; note where INBOX stands; then write the password,
    the config section and the baseline in the index. A failed login returns
    `{"ok": false, "step", "message", "hint"}` and writes nothing; any later failure undoes
    what was written and raises MailError. Success: `{"ok": true, "mailbox", "address",
    "provider"}`.

    The baseline makes all mail already in INBOX old: last_uid is UIDNEXT-1, and
    backfill_before is the lowest UID of mail from the last 90 days (None when there is none),
    for the poll to index as old mail.
    """
    hub, mail_dir, now = Path(hub), Path(mail_dir), _now(now)
    config._check_name(name)
    address = address.strip()
    if not ADDRESS.fullmatch(address):
        raise MailError(f"not an email address: {address!r}")
    _check_password(password)
    settings = config.load(mail_dir)
    if name in settings.mailboxes:
        raise MailError(f'there is already a mailbox named "{name}"')
    for other in settings.mailboxes.values():
        if same_address(other.address, address):
            raise MailError(f'{address} is already added, as "{other.name}"')
    if provider is not None:
        found = PROVIDERS.get(provider)
        if found is None:
            raise MailError(
                f"unknown provider {provider!r}; the supported ones are {', '.join(PROVIDERS)}")
    else:
        detected = providers.detect(address, mx=mx or providers.lookup_mx)
        if not detected["supported"]:
            raise MailError(detected["reason"])
        found = PROVIDERS[detected["provider"]]
    mb = Mailbox(
        name=name, address=address, provider=found.key, imap=found.imap, smtp=found.smtp,
        added=now.astimezone().isoformat(timespec="seconds"))
    config.add_mailbox_text(config.read_text(mail_dir), mb)  # the section can be written

    srv = (connect or server.connect)(mb, password)
    try:
        srv.check_login()
        uidvalidity, uidnext = srv.inbox_status()
        last_uid = max(uidnext - 1, 0)
        since = now.astimezone(UTC).date() - timedelta(days=RECENT_DAYS)
        recent = [uid for uid in srv.uids_since(since) if uid <= last_uid]
    except LoginError as err:
        return _login_failed(err, mb, password)
    except MailError as err:
        raise MailError(f"logged in to {address}, but reading INBOX failed: {err}") from None
    finally:
        srv.close()

    undo: list[Callable[[], None]] = []
    try:
        undo.append(_password_undo(hub, address))
        _write_password(hub, address, password)
        before = config.read_text(mail_dir)
        written = config.add_mailbox_text(before, mb)
        config.write_text(mail_dir, written)
        undo.append(lambda: _remove_section(mail_dir, name, written, before))
        with Store.open(mail_dir) as store:
            store.forget(address)  # nothing from an earlier mailbox of this address stays
            store.set_baseline(address, uidvalidity, last_uid, min(recent) if recent else None)
    except BaseException as err:
        _rolled_back(err, undo)
        raise
    return {"ok": True, "mailbox": name, "address": address, "provider": found.key}


def _remove_section(mail_dir: Path, name: str, written: str, before: str) -> None:
    """Undo the add of a section: the file as it was, or, when it was edited since, the file
    without the section."""
    now_text = config.read_text(mail_dir)
    if now_text == written:
        config.write_text(mail_dir, before)
    else:
        config.write_text(mail_dir, config.remove_mailbox_text(now_text, name))


def test(hub: Path, mail_dir: Path, name: str, *, connect=None) -> dict:
    """Log in to both servers of mailbox `name` with its stored password:
    `{"ok": true, "mailbox", "address"}`, or the failure as `add` returns it."""
    _, mb = _mailbox(Path(mail_dir), name)
    password = read_password(Path(hub), mb.address)
    srv = (connect or server.connect)(mb, password)
    try:
        srv.check_login()
    except LoginError as err:
        return _login_failed(err, mb, password)
    finally:
        srv.close()
    return {"ok": True, "mailbox": name, "address": mb.address}


def edit(hub: Path, mail_dir: Path, name: str, *, new_name: str | None = None,
         password: str | None = None, paused: bool | None = None, connect=None) -> dict:
    """Change mailbox `name`: a new password (checked by logging in to both servers before it
    is stored), a new short name, paused or not. A failed login returns the failure as `add`
    does and changes nothing. Success: `{"ok": true, "mailbox": <its name now>, "address",
    "changed": [...]}` with "password", "name", "paused" for what changed."""
    hub, mail_dir = Path(hub), Path(mail_dir)
    if new_name is None and password is None and paused is None:
        raise MailError("nothing to change: give a new name, a new password, or pause or resume")
    settings, mb = _mailbox(mail_dir, name)
    if new_name is not None:
        config._check_name(new_name)
        if new_name != name and new_name in settings.mailboxes:
            raise MailError(f'there is already a mailbox named "{new_name}"')
    if password is not None:
        _check_password(password)

    def edited(text: str) -> tuple[str, list[str], str]:
        changed, final = [], name
        if password is not None:
            changed.append("password")
        if new_name is not None and new_name != name:
            text = config.rename_mailbox_text(text, name, new_name)
            changed.append("name")
            final = new_name
        if paused is not None and paused != config.parse(text).mailboxes[final].paused:
            text = config.set_mailbox_value_text(text, final, "paused", True if paused else None)
            changed.append("paused")
        return text, changed, final

    edited(config.read_text(mail_dir))  # refuses an edit config.toml cannot take, before login
    if password is not None:
        srv = (connect or server.connect)(mb, password)
        try:
            srv.check_login()
        except LoginError as err:
            return _login_failed(err, mb, password)
        finally:
            srv.close()

    undo: list[Callable[[], None]] = []
    try:
        if password is not None:
            undo.append(_password_undo(hub, mb.address))
            _write_password(hub, mb.address, password)
        before = config.read_text(mail_dir)
        text, changed, final = edited(before)
        if text != before:
            config.write_text(mail_dir, text)
    except BaseException as err:
        _rolled_back(err, undo)
        raise
    return {"ok": True, "mailbox": final, "address": mb.address, "changed": changed}


def remove(hub: Path, mail_dir: Path, name: str) -> dict:
    """Stop managing mailbox `name`: its config section (and so its own rules) goes, its stored
    password is deleted, and the index forgets it. Downloaded attachments stay.
    `{"ok": true, "mailbox", "address", "password_deleted": bool}`."""
    hub, mail_dir = Path(hub), Path(mail_dir)
    settings, mb = _mailbox(mail_dir, name)
    new_text = config.remove_mailbox_text(config.read_text(mail_dir), name)
    # a second section for the same address (a hand edit) still needs the password and index
    shared = any(same_address(other.address, mb.address)
                 for other in settings.mailboxes.values() if other.name != name)
    if not shared:
        # first, so that a busy index fails the command before anything is gone; a mailbox
        # that stays because a later step fails is indexed afresh by the next poll
        with Store.open(mail_dir) as store:
            store.forget(mb.address)
    config.write_text(mail_dir, new_text)
    deleted = False
    if not shared:
        path = secret_path(hub, mb.address)
        try:
            path.unlink()
            deleted = True
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise MailError(
                f"the mailbox is removed, but its password file {path} could not be deleted "
                f"({exc.strerror or exc}); delete it by hand") from None
    return {"ok": True, "mailbox": name, "address": mb.address, "password_deleted": deleted}


def list_(hub: Path, mail_dir: Path, *, page: int = 1, per_page: int = PER_PAGE) -> dict:
    """The mailboxes in config order, `per_page` to a page:
    `{"items": [{"name", "address", "provider", "paused", "last_ok"}], "total", "next_page"}`.
    `last_ok` is when the poll last reached the mailbox (None: not yet)."""
    if page < 1 or per_page < 1:
        raise MailError("page and per_page must be at least 1")
    boxes = list(config.load(Path(mail_dir)).mailboxes.values())
    shown = boxes[(page - 1) * per_page : page * per_page]
    items = []
    if shown:
        with Store.open(Path(mail_dir)) as store:
            items = [{
                "name": mb.name, "address": mb.address, "provider": mb.provider,
                "paused": mb.paused, "last_ok": store.last_ok(mb.address),
            } for mb in shown]
    return {
        "items": items, "total": len(boxes),
        "next_page": page + 1 if page * per_page < len(boxes) else None,
    }

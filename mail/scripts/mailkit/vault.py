"""The secrets of the mail app: passwords and OAuth tokens, kept in the state directory and never
in the hub.

A password is one file, `secrets/<mailbox>`, holding the text and nothing else (no line break). An
OAuth token is `secrets/<mailbox>.oauth.json`, the JSON that ortie hands over. Nothing here prints
one: only `secret-fetch` and `token-store read` do, for himalaya, carillon and ortie.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from .context import Ctx
from .paths import MACHINE_ID, POSIX, write_private
from .settings import NAME_RE, MailError

LOCK_NAME = "lock"  # the state lock (paths.locked): not data, and held while we work


def _is_console(fd: int) -> bool:
    """Windows: is the file descriptor a console? (isatty() says yes to the NUL device as well.)"""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    get_console_mode = ctypes.WinDLL("kernel32", use_last_error=True).GetConsoleMode
    get_console_mode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_console_mode.restype = wintypes.BOOL
    return bool(get_console_mode(msvcrt.get_osfhandle(fd), ctypes.byref(wintypes.DWORD())))


def at_terminal() -> bool:
    """Is standard input a person's terminal? Only then may a secret be asked for: it is typed
    there, so it cannot come from the chat, a pipe or a file, nor from an agent that has no
    terminal (a harness that closes stdin leaves the NUL device on Windows, which counts as a
    terminal to isatty() and would make getpass wait on the console)."""
    try:
        if not sys.stdin.isatty():
            return False
        return _is_console(sys.stdin.fileno()) if sys.platform == "win32" else True
    except (AttributeError, OSError, ValueError):  # no stdin, or one without a descriptor
        return False


def _check(mailbox: str) -> str:
    """A mailbox name is a file name here: only what settings accepts as a name gets through."""
    if not NAME_RE.fullmatch(mailbox):
        raise MailError(f"{mailbox!r} is not a mailbox name")
    return mailbox


def secret_path(state: Path, mailbox: str) -> Path:
    return Path(state) / "secrets" / _check(mailbox)


def _put(path: Path, data: bytes, mailbox: str) -> None:
    """Write a secret file into `secrets/`, which is made private first."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if POSIX:
            path.parent.chmod(0o700)
        write_private(path, data)
    except OSError as err:
        raise MailError(f"cannot store the secret of {mailbox}: {err}") from err


def store(state: Path, mailbox: str, value: str) -> None:
    """Keep `value` for `mailbox`, without the whitespace around it. The old secret stays until
    the new one is whole."""
    path = secret_path(state, mailbox)
    secret = value.strip()
    if not secret:
        raise MailError("a secret cannot be empty")
    if "\n" in secret or "\r" in secret:
        # The tools read a password command up to its first line: the rest would be lost.
        raise MailError("a secret cannot span several lines")
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if POSIX:
            path.parent.chmod(0o700)
        write_private(path, secret.encode("utf-8"))
    except OSError as err:
        raise MailError(f"cannot store the secret of {mailbox}: {err}") from err


def read(state: Path, mailbox: str) -> str | None:
    """The secret of `mailbox`, or None when none is stored. For `secret-fetch` only."""
    path = secret_path(state, mailbox)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as err:
        raise MailError(f"cannot read the secret of {mailbox}: {err}") from err
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:  # the error would quote a byte of it
        raise MailError(f"the secret of {mailbox} is not UTF-8 text: set it again") from None


def exists(state: Path, mailbox: str) -> bool:
    return secret_path(state, mailbox).is_file()


def remove(state: Path, mailbox: str) -> bool:
    """Delete the secrets of `mailbox`: its password and its OAuth token, so that deleting an
    Outlook mailbox's secret drops the refresh token too. True if there was either."""
    existed = False
    for path in (secret_path(state, mailbox), token_path(state, mailbox)):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as err:
            raise MailError(f"cannot remove the secret of {mailbox}: {err}") from err
        existed = True
    return existed


def token_path(state: Path, mailbox: str) -> Path:
    """Where ortie's token of `mailbox` is kept: next to the passwords, never in the generated
    configs."""
    base = secret_path(state, mailbox)
    return base.with_name(f"{base.name}.oauth.json")


def store_token(state: Path, mailbox: str, data: bytes) -> None:
    """Keep the token JSON ortie wrote. Anything else is refused, and the old token stays; the
    message never quotes `data`."""
    path = token_path(state, mailbox)
    try:
        json.loads(data.decode("utf-8"))
    except ValueError:  # not UTF-8, or not JSON
        raise MailError("the token is not JSON: nothing stored") from None
    _put(path, data, mailbox)


def read_token(state: Path, mailbox: str) -> str | None:
    """The token JSON of `mailbox`, or None when none is stored. For `token-store read` only."""
    path = token_path(state, mailbox)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as err:
        raise MailError(f"cannot read the token of {mailbox}: {err}") from err
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:  # the error would quote a byte of it
        raise MailError(f"the token of {mailbox} is not UTF-8 text: sign in again") from None


def has_token(state: Path, mailbox: str) -> bool:
    return token_path(state, mailbox).is_file()


def mx_command(ctx: Ctx, *args: str) -> list[str]:
    """The argv that runs `mx <args>` for this hub: absolute paths only, and no shell, so it means
    the same on every OS and from every tool that runs it (the F-AUTH password command)."""
    uv = shutil.which("uv")
    if uv is None:
        raise MailError("uv is not on PATH: the tools that fetch secrets run mx through uv")
    if not os.path.isabs(uv):  # which() returns what PATH holds, a relative entry included
        uv = os.path.abspath(uv)
    return [uv, "run", str(ctx.app / "scripts" / "mx.py"), "--hub", str(ctx.hub), *args]


def fetch_command(ctx: Ctx, mailbox: str) -> list[str]:
    """How a password is fetched, for the generated himalaya and carillon configs. The one place
    that says so: a password app that comes later replaces this function and nothing else."""
    return mx_command(ctx, "secret-fetch", mailbox)


def forget(state: Path, *, secrets: bool) -> list[str]:
    """Empty the state directory, except the lock the caller holds and, unless `secrets`,
    `secrets/` and this machine's id (`paths.this_machine`: the alert switch names the machine by
    it). Returns the names it removed, sorted, relative to the directory."""
    removed: list[str] = []
    kept = () if secrets else ("secrets", MACHINE_ID)
    for entry in sorted(Path(state).iterdir(), key=lambda p: p.name):
        if entry.name == LOCK_NAME or entry.name in kept:
            continue
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError as err:  # on Windows: a file the watch service still holds open
            raise MailError(f"cannot remove {entry.name} from {state}: {err}") from err
        removed.append(entry.name)
    return removed

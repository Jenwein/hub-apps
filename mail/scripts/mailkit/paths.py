"""The per-machine state of the mail app: where it lives, private files, and the lock.

Secrets, generated configs, drafts and marks live outside the hub, in a directory only the user
can read, and are never committed.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

from .settings import MailError

STATE_ENV = "CC_HUB_MAIL_STATE"
POSIX = os.name == "posix"  # permission bits mean something only here

LOCK_POLL_SECONDS = 0.2
LOCK_STALE_SECONDS = 120.0


def _state_base() -> Path:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) if local else Path.home() / "AppData" / "Local"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg and Path(xdg).is_absolute():
        return Path(xdg)
    return Path.home() / ".local" / "state"


def state_dir(hub: Path) -> Path:
    """The state directory of this hub on this machine, created with mode 0700 (POSIX).

    `$CC_HUB_MAIL_STATE` names it directly; otherwise it is `<base>/cc-hub-mail/<id>`, where the
    id is made from the resolved path of the hub, so two hubs on one machine do not share state.
    """
    override = os.environ.get(STATE_ENV)
    if override:
        path = Path(override).absolute()
    else:
        digest = hashlib.sha256(str(Path(hub).resolve()).encode("utf-8")).hexdigest()[:12]
        path = _state_base() / "cc-hub-mail" / digest
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if POSIX:
            path.chmod(0o700)  # mkdir's mode is cut by the umask and ignored for an old directory
    except OSError as err:
        raise MailError(f"cannot create the mail state directory {path}: {err}") from err
    return path


def write_private(path: Path, data: bytes) -> None:
    """Write `data` to `path` so that readers see the old file or the whole new one.

    The new file is made in the same folder (mkstemp gives it mode 0600 on POSIX) and then
    replaces `path`, so a crash leaves no half-written file.
    """
    path = Path(path)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


def read_json(path: Path, default):
    """The JSON in `path`, or `default` if there is no such file."""
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as err:  # ValueError: bad JSON, or bytes that are not UTF-8
        raise MailError(f"cannot read {path}: {err}") from err


def write_json(path: Path, data) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    write_private(path, text.encode("utf-8"))


# A taken lock file: on Windows a file that is being deleted refuses a new one with a
# PermissionError instead.
_HELD = (FileExistsError, PermissionError) if sys.platform == "win32" else (FileExistsError,)


def _stale(lock: Path) -> bool:
    try:
        return time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS
    except FileNotFoundError:
        return True  # released meanwhile: try again at once


@contextlib.contextmanager
def locked(state: Path, timeout: float = 30.0) -> Iterator[None]:
    """Hold the lock of the state directory: one writer of drafts, marks and the like at a time.

    The lock is the file `state/lock`, made with O_EXCL. A caller that finds it taken tries again
    every 0.2 s for `timeout` seconds, then fails with a MailError. A lock older than 120 s
    belongs to a process that died, and is taken over.
    """
    lock = Path(state) / "lock"
    token = f"{os.getpid()}-{uuid.uuid4().hex}"  # tells our lock from the one that replaced it
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except _HELD:
            if _stale(lock):
                with contextlib.suppress(OSError):
                    lock.unlink()
                continue
            if time.monotonic() >= deadline:
                raise MailError("the mail state is busy") from None
            time.sleep(LOCK_POLL_SECONDS)
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(token)
            break
    try:
        yield
    finally:
        # A holder that outlived the stale limit must not delete the lock of its successor.
        with contextlib.suppress(OSError, ValueError):
            if lock.read_text("utf-8") == token:
                lock.unlink()

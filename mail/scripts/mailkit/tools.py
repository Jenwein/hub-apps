"""The external tools of the mail app (himalaya, carillon, ortie): pinned and downloaded.

Each tool is pinned to a release tag and, for every platform, to the SHA-256 of its release
archive. `install_tool` downloads from the official GitHub release only and checks the hash
before it opens the archive; it writes the one binary and nothing else.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import os
import platform
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import urllib.request
import zlib
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .settings import MailError

# mx's platform key -> the suffix upstream puts in its asset names.
PLATFORMS = {
    "linux-x86_64": "x86_64-linux",
    "linux-aarch64": "aarch64-linux",
    "macos-x86_64": "x86_64-darwin",
    "macos-aarch64": "aarch64-darwin",
    "windows-x86_64": "x86_64-windows",
}

# Every mailkit module that spawns a subprocess whose output it captures passes
# `creationflags=NO_WINDOW`: a hidden console on Windows, 0 elsewhere.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill a program mx started and what it started in turn (himalaya's password command): a
    child that lives on would keep our pipes open. POSIX: the process group, so the program must
    run in its own session (`start_new_session=True`). Windows: taskkill walks the tree."""
    if os.name == "posix":
        with suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    else:
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)], check=False,
                stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                creationflags=NO_WINDOW)
    with suppress(OSError):
        proc.kill()
    with suppress(OSError, subprocess.SubprocessError):
        proc.communicate(timeout=10)


@dataclass(frozen=True)
class Asset:
    file: str  # the file name in the release
    sha256: str
    member: str  # the path of the binary inside the archive


@dataclass(frozen=True)
class Tool:
    name: str
    repo: str  # owner/name on GitHub
    version: str  # the release tag
    binary: str  # the executable's name, without .exe
    assets: dict[str, Asset]  # by platform key


def _pinned(name: str, version: str, hashes: dict[str, str]) -> Tool:
    """The archives hold the binary at their root (`<name>`, `<name>.exe` on Windows), next to
    a share/ folder: read from all fifteen pinned archives."""
    assets = {
        key: Asset(
            f"{name}.{suffix}.tgz", hashes[key],
            f"{name}.exe" if key.startswith("windows") else name)
        for key, suffix in PLATFORMS.items()
    }
    return Tool(name, f"pimalaya/{name}", version, name, assets)


# GitHub's per-asset `digest` of each release, which matched the archives downloaded on
# 2026-10-19. The releases are mutable: a digest that differs is a reason to stop and ask, not
# to update this table.
TOOLS: dict[str, Tool] = {
    "himalaya": _pinned("himalaya", "v2.2.1", {
        "linux-x86_64": "5c5ba2724c162f82d0a0c71b6c03224ed44f8bef7b14ace5da0635d3af2665a0",
        "linux-aarch64": "1dc21c3dd6d948929e22e5cae49b9d0b3b30ff88fafd4493fd4460c6252e9d90",
        "macos-x86_64": "24e226bc6bccfeadc89608953ffc0fcb46623d1d4cd4689734cbace243cc8d2e",
        "macos-aarch64": "a5d97a1f7bbca45e58bddde3f9f17325f614c9cd6d5f17fabf38ddcb030869ab",
        "windows-x86_64": "abea8ecbca5fbd566860941bbab91e40f1dde57e737091a451208b8c61b99523",
    }),
    "carillon": _pinned("carillon", "v0.2.0", {
        "linux-x86_64": "a4edea7fdc79f3a0461b4a3014b31f68d6ecdfcd1c640afef7a6948700b3d6c5",
        "linux-aarch64": "15ac6ee2df46d18df6d2ce4e3782e34b1733cc74c72fc32b0bc14deca0faeb7e",
        "macos-x86_64": "fd79bf68c76c8eec87e13db42675c8d14b2b1537aec8eda6d755a495feecfc2f",
        "macos-aarch64": "a52cedac61b22576afe22d8970c24a336d055867cf7d7412156cdb9627acf2bf",
        "windows-x86_64": "3d2dd1e285775f69d66ecf986df0c1cd7bbcac42076884e333a0293b2471a028",
    }),
    "ortie": _pinned("ortie", "v2.3.0", {
        "linux-x86_64": "1b421f28cb2bc7e6acfe964df548d568ddbab359bc791c43e5c4f7b091910f10",
        "linux-aarch64": "f12484164629fe6fc3f8c59a1989e48f1d05b97ecbffe7c2bb3ee33eba20934e",
        "macos-x86_64": "6761c5f159fa74bafd1cd1674e7cecaf478f58e556b8894634e3368fa20caef3",
        "macos-aarch64": "f03a29017684d3aee0e160723ce73c5667d4abf9ee44278ac30e5765919f3fa7",
        "windows-x86_64": "aff41ae32060c656d1b1ac60a8ddc028836c0866d6541c1392ee3205d69acfa3",
    }),
}

_SYSTEMS = {"linux": "linux", "darwin": "macos", "windows": "windows"}
_MACHINES = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}


def platform_key(
    system: str | None = None, machine: str | None = None, tool: str | None = None
) -> str:
    """The key in PLATFORMS of this machine (or of the given system and machine, as
    platform.system() and platform.machine() print them). `tool` only names the tool in the
    error."""
    system = platform.system() if system is None else system
    machine = platform.machine() if machine is None else machine
    os_name = _SYSTEMS.get(system.lower())
    arch = _MACHINES.get(machine.lower())
    if os_name is None or arch is None or f"{os_name}-{arch}" not in PLATFORMS:
        raise MailError(f"no {tool + ' ' if tool else ''}build for {system} {machine}")
    return f"{os_name}-{arch}"


def fetch_url(url: str, timeout: float = 120) -> bytes:
    """The body of `url`, through the proxies the environment names (urllib's default)."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def _known(name: str) -> Tool:
    try:
        return TOOLS[name]
    except KeyError:
        raise MailError(f"unknown tool {name!r} (known: {', '.join(TOOLS)})") from None


def _binary_of(archive: bytes, asset: Asset) -> bytes:
    """The bytes of the archive's binary. Nothing else in the archive is read, let alone
    extracted, so no name inside it can lead anywhere."""
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            try:
                info = tar.getmember(asset.member)
            except KeyError:
                info = None
            stream = tar.extractfile(info) if info is not None and info.isreg() else None
            if stream is None:
                raise MailError(f"{asset.file} has no file {asset.member}")
            return stream.read()
    except (tarfile.TarError, EOFError, zlib.error) as err:
        raise MailError(f"{asset.file} is not a readable archive: {err}") from err


def _write_executable(target: Path, data: bytes) -> None:
    """Write `data` to `target` through a temp file in the same folder, so that an interrupted
    install leaves the old binary (or nothing) and never half of the new one."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".part", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.chmod(tmp, 0o755)
        os.replace(tmp, target)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


def install_tool(
    name: str,
    dest: Path,
    *,
    fetch: Callable[[str], bytes] = fetch_url,
    key: str | None = None,
) -> Path:
    """Download the pinned release of `name` for this machine (or platform `key`), check its
    SHA-256 and write the binary to `dest`. Returns the path of the binary."""
    tool = _known(name)
    key = key or platform_key(tool=name)
    asset = tool.assets.get(key)
    if asset is None:
        raise MailError(f"no {name} build for {key}")
    url = f"https://github.com/{tool.repo}/releases/download/{tool.version}/{asset.file}"
    try:
        archive = fetch(url)
    except (OSError, http.client.HTTPException) as err:
        raise MailError(f"could not download {asset.file} ({url}): {err}") from err
    got = hashlib.sha256(archive).hexdigest()
    if got != asset.sha256:
        raise MailError(
            f"checksum mismatch for {asset.file}: expected {asset.sha256}, got {got}; "
            "nothing was installed")
    binary = _binary_of(archive, asset)
    target = Path(dest) / (tool.binary + (".exe" if key.startswith("windows") else ""))
    try:
        _write_executable(target, binary)
    except OSError as err:
        raise MailError(f"cannot write {target}: {err}") from err
    return target


_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


def _version_of(exe: str) -> str | None:
    """The version that `exe --version` prints, or None."""
    try:
        done = subprocess.run(
            [exe, "--version"], check=False, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=30, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = _VERSION_RE.search(f"{done.stdout}\n{done.stderr}")
    return found.group() if found else None


def installed_version(name: str) -> str | None:
    """The version the tool on PATH prints for `--version`; None when it is not on PATH or
    prints none."""
    tool = _known(name)
    exe = shutil.which(tool.binary)  # a .cmd shim on Windows cannot be started by bare name
    return None if exe is None else _version_of(exe)


@dataclass(frozen=True)
class VersionProblem:
    """A tool on PATH that is not the pinned release, or no tool on PATH."""

    tool: str
    found: str | None  # the version on PATH: None when it is not on PATH, "" when it says none
    pinned: str  # the pinned version, without the tag's `v`
    path: str = ""  # where it was found

    @property
    def key(self) -> str:
        """The problem in a word, to tell whether it was told before."""
        shown = "missing" if self.found is None else self.found or "unknown"
        return f"{self.tool} {shown}"

    def message(self) -> str:
        """The problem and its fix, in English (for the agent and the log)."""
        fix = (
            f"run uv run apps/mail/scripts/mx.py install-tool {self.tool}, and put .local/bin of "
            "your home directory first on PATH")
        if self.found is None:
            return f"{self.tool} is not on PATH, and the mail app needs {self.pinned}: {fix}"
        if not self.found:
            return (
                f"{self.tool} on PATH ({self.path}) does not say its version, and the mail app "
                f"needs {self.pinned}: {fix}")
        return (
            f"{self.tool} on PATH ({self.path}) is {self.found}, but the mail app needs "
            f"{self.pinned}: {fix}")


def version_problem(name: str) -> VersionProblem | None:
    """None when the tool on PATH is the pinned release of `name`; else what is wrong. A tool
    of another version may print other JSON, or none, so mx does not run it on a mailbox."""
    tool = _known(name)
    pinned = tool.version.removeprefix("v")
    exe = shutil.which(tool.binary)
    if exe is None:
        return VersionProblem(name, None, pinned)
    found = _version_of(exe) or ""
    return None if found == pinned else VersionProblem(name, found, pinned, exe)

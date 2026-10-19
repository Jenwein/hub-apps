import hashlib
import io
import json
import os
import re
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest
from mailkit import tools
from mailkit.cli import main
from mailkit.settings import MailError
from mailkit.tools import PLATFORMS, TOOLS, Asset, Tool, install_tool, platform_key
from mailtest import ROOT

APP_TOML = ROOT / "mail" / "app.toml"
POSIX = os.name == "posix"
RELEASE = "https://github.com/pimalaya/{tool}/releases/download/{tag}/{file}"


def tgz(members: dict[str, bytes], *, links: dict[str, str] | None = None) -> bytes:
    """A .tgz holding the given regular files (and symlinks name -> target), in that order."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644  # the installer sets the mode itself
            tar.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
    return buf.getvalue()


def pin(monkeypatch, archive: bytes, *, name: str = "himalaya") -> None:
    """Make `archive` the release asset of `name` on every platform."""
    assets = {
        key: Asset(
            f"{name}.{suffix}.tgz",
            hashlib.sha256(archive).hexdigest(),
            f"{name}.exe" if key.startswith("windows") else name,
        )
        for key, suffix in PLATFORMS.items()
    }
    monkeypatch.setitem(TOOLS, name, Tool(name, f"pimalaya/{name}", "v2.2.1", name, assets))


def serve(monkeypatch, data: bytes) -> None:
    """Every download through urllib answers with `data`."""
    monkeypatch.setattr(tools.urllib.request, "urlopen",
                        lambda url, timeout=None: io.BytesIO(data))


def files(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


# --- the pins ---


def test_pins_are_complete():
    assert {t: TOOLS[t].version for t in TOOLS} == {
        "himalaya": "v2.2.1", "carillon": "v0.2.0", "ortie": "v2.3.0"}
    for tool in TOOLS.values():
        assert set(tool.assets) == set(PLATFORMS)
        assert all(a.file.endswith(".tgz") for a in tool.assets.values())
        assert all(re.fullmatch(r"[0-9a-f]{64}", a.sha256) for a in tool.assets.values())
    assert TOOLS["himalaya"].assets["linux-x86_64"].sha256.startswith("5c5ba272")


def test_each_binary_sits_at_the_archive_root():
    # Read from the real archives on 2026-10-19: `<tool>` or `<tool>.exe`, next to share/.
    for name, tool in TOOLS.items():
        assert tool.binary == name
        for key, asset in tool.assets.items():
            assert asset.member == (f"{name}.exe" if key.startswith("windows") else name)


@pytest.mark.parametrize("name", sorted(TOOLS))
@pytest.mark.parametrize("key", sorted(PLATFORMS))
def test_url_is_the_official_release(name, key, tmp_path):
    urls = []

    def fetch(url):
        urls.append(url)
        return b"not the archive"

    with pytest.raises(MailError, match="checksum"):
        install_tool(name, tmp_path / "bin", fetch=fetch, key=key)
    tool = TOOLS[name]
    assert urls == [RELEASE.format(tool=name, tag=tool.version, file=tool.assets[key].file)]
    assert urls[0] == (
        f"https://github.com/pimalaya/{name}/releases/download/{tool.version}"
        f"/{name}.{PLATFORMS[key]}.tgz")


# --- the platform ---


@pytest.mark.parametrize(("system", "machine", "key"), [
    ("Linux", "x86_64", "linux-x86_64"),
    ("Linux", "aarch64", "linux-aarch64"),
    ("Linux", "arm64", "linux-aarch64"),
    ("Darwin", "x86_64", "macos-x86_64"),
    ("Darwin", "arm64", "macos-aarch64"),
    ("Windows", "AMD64", "windows-x86_64"),
    ("Windows", "x86_64", "windows-x86_64"),
])
def test_platform_key(system, machine, key):
    assert platform_key(system, machine) == key


def test_platform_key_of_this_machine(monkeypatch):
    monkeypatch.setattr(tools.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(tools.platform, "machine", lambda: "arm64")
    assert platform_key() == "macos-aarch64"


def test_unknown_platform():
    with pytest.raises(MailError):
        platform_key("FreeBSD", "x86_64")
    with pytest.raises(MailError, match="Windows ARM64"):  # no such build
        platform_key("Windows", "ARM64")
    with pytest.raises(MailError, match="Linux armv7l"):
        platform_key("Linux", "armv7l")


def test_unknown_platform_names_the_tool(tmp_path, monkeypatch):
    with pytest.raises(MailError, match=r"no ortie build for FreeBSD x86_64"):
        platform_key("FreeBSD", "x86_64", tool="ortie")
    monkeypatch.setattr(tools.platform, "system", lambda: "FreeBSD")
    monkeypatch.setattr(tools.platform, "machine", lambda: "amd64")
    with pytest.raises(MailError, match=r"no carillon build for FreeBSD amd64"):
        install_tool("carillon", tmp_path, fetch=lambda url: pytest.fail("downloaded"))
    assert files(tmp_path) == []


# --- install_tool ---


def test_install_checks_the_hash_before_unpacking(tmp_path, monkeypatch):
    pin(monkeypatch, tgz({"himalaya": b"the real binary"}))
    dest = tmp_path / "bin"
    with pytest.raises(MailError, match="checksum") as err:
        install_tool("himalaya", dest, fetch=lambda url: tgz({"himalaya": b"a trojan"}),
                     key="linux-x86_64")
    assert "himalaya.x86_64-linux.tgz" in str(err.value)
    assert files(dest) == []  # nothing written, not even the folder's temp files

    dest.mkdir()  # an existing, empty folder stays empty too
    with pytest.raises(MailError, match="checksum"):
        install_tool("himalaya", dest, fetch=lambda url: b"", key="linux-x86_64")
    assert files(dest) == []


def test_install_writes_only_the_binary(tmp_path, monkeypatch):
    archive = tgz({
        "../evil": b"outside",
        "/abs-evil": b"absolute",
        "himalaya": b"the binary",
        "share/man/himalaya.1.gz": b"man",
        "share/schemas/himalaya-message-send.json": b"{}",
    })
    pin(monkeypatch, archive)
    dest = tmp_path / "deep" / "bin"  # created on the way
    path = install_tool("himalaya", dest, fetch=lambda url: archive, key="linux-x86_64")
    assert path == dest / "himalaya"
    assert path.read_bytes() == b"the binary"
    assert files(dest) == ["himalaya"]
    assert not (tmp_path / "deep" / "evil").exists() and not (tmp_path / "evil").exists()
    if POSIX:
        assert path.stat().st_mode & 0o777 == 0o755


def test_windows_gets_the_exe(tmp_path, monkeypatch):
    archive = tgz({"himalaya": b"elf", "himalaya.exe": b"pe", "share/x": b"."})
    pin(monkeypatch, archive)
    path = install_tool("himalaya", tmp_path, fetch=lambda url: archive, key="windows-x86_64")
    assert path == tmp_path / "himalaya.exe"
    assert path.read_bytes() == b"pe"
    assert files(tmp_path) == ["himalaya.exe"]


def test_install_replaces_an_older_binary(tmp_path, monkeypatch):
    old = tmp_path / "himalaya"
    old.write_bytes(b"old")
    archive = tgz({"himalaya": b"new"})
    pin(monkeypatch, archive)
    install_tool("himalaya", tmp_path, fetch=lambda url: archive, key="linux-x86_64")
    assert old.read_bytes() == b"new"
    assert files(tmp_path) == ["himalaya"]  # no temp file left behind


def test_install_picks_the_pinned_asset_of_the_platform(tmp_path, monkeypatch):
    archive = tgz({"himalaya": b"x"})
    pin(monkeypatch, archive)
    seen = []
    install_tool("himalaya", tmp_path, fetch=lambda url: seen.append(url) or archive,
                 key="macos-aarch64")
    assert seen == [RELEASE.format(tool="himalaya", tag="v2.2.1",
                                   file="himalaya.aarch64-darwin.tgz")]


def test_install_uses_this_machine_by_default(tmp_path, monkeypatch):
    archive = tgz({"himalaya": b"x", "himalaya.exe": b"x"})
    pin(monkeypatch, archive)
    monkeypatch.setattr(tools.platform, "system", lambda: "Windows")
    monkeypatch.setattr(tools.platform, "machine", lambda: "AMD64")
    path = install_tool("himalaya", tmp_path, fetch=lambda url: archive)
    assert path.name == "himalaya.exe"


def test_missing_member_installs_nothing(tmp_path, monkeypatch):
    archive = tgz({"share/x": b"."})
    pin(monkeypatch, archive)
    dest = tmp_path / "bin"
    with pytest.raises(MailError, match="himalaya"):
        install_tool("himalaya", dest, fetch=lambda url: archive, key="linux-x86_64")
    assert files(dest) == []


def test_a_link_is_not_the_binary(tmp_path, monkeypatch):
    archive = tgz({"share/x": b"decoy"}, links={"himalaya": "share/x"})
    pin(monkeypatch, archive)
    with pytest.raises(MailError, match="himalaya"):
        install_tool("himalaya", tmp_path / "bin", fetch=lambda url: archive, key="linux-x86_64")
    assert files(tmp_path / "bin") == []


def test_a_broken_download_is_a_mail_error(tmp_path):
    def fetch(url):
        raise OSError("connection reset")

    with pytest.raises(MailError, match=r"himalaya\.x86_64-linux\.tgz.*connection reset"):
        install_tool("himalaya", tmp_path / "bin", fetch=fetch, key="linux-x86_64")
    assert files(tmp_path / "bin") == []


def test_unknown_tool(tmp_path):
    with pytest.raises(MailError, match="unknown tool"):
        install_tool("curl", tmp_path)


def test_fetch_url_goes_through_urllib(monkeypatch):
    seen = {}

    def urlopen(url, timeout=None):
        seen.update(url=url, timeout=timeout)
        return io.BytesIO(b"payload")

    monkeypatch.setattr(tools.urllib.request, "urlopen", urlopen)
    assert tools.fetch_url("https://example.invalid/x.tgz") == b"payload"
    assert seen == {"url": "https://example.invalid/x.tgz", "timeout": 120}


# --- installed_version ---


def test_installed_version(make_exe):
    make_exe("himalaya", "print('himalaya 2.2.1 +imap +smtp')")
    assert tools.installed_version("himalaya") == "2.2.1"


def test_installed_version_takes_the_first_number(make_exe):
    make_exe("carillon", "print('carillon v0.2.0 (rustc 1.90.0)')")
    assert tools.installed_version("carillon") == "0.2.0"


def test_not_installed_or_no_version(make_exe, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert tools.installed_version("himalaya") is None
    make_exe("ortie", "print('ortie dev build')")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert tools.installed_version("ortie") is None


def test_installed_version_runs_without_a_console_window(make_exe, monkeypatch):
    make_exe("himalaya", "print('himalaya 2.2.1')")
    seen = {}
    real_run = tools.subprocess.run

    def run(*args, **kwargs):
        seen.update(kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(tools.subprocess, "run", run)
    assert tools.installed_version("himalaya") == "2.2.1"
    assert seen["creationflags"] == tools.NO_WINDOW


def test_installed_version_of_an_unknown_tool():
    with pytest.raises(MailError, match="unknown tool"):
        tools.installed_version("curl")


# --- the command ---


def test_install_tool_command_needs_no_hub(tmp_path, monkeypatch, capsys):
    """R1: no mail/mailboxes.toml here, and the working directory is not a hub."""
    archive = tgz({"himalaya": b"bin", "himalaya.exe": b"exe"})
    pin(monkeypatch, archive)
    work = tmp_path / "elsewhere"
    work.mkdir()
    monkeypatch.chdir(work)
    serve(monkeypatch, archive)
    dest = tmp_path / "bin"
    assert main(["install-tool", "himalaya", "--dir", str(dest)]) == 0
    out = json.loads(capsys.readouterr().out)
    exe = "himalaya.exe" if sys.platform == "win32" else "himalaya"
    assert out == {"installed": str(dest / exe), "version": "v2.2.1"}
    assert files(dest) == [exe]
    assert files(work) == []


def test_install_tool_command_defaults_to_local_bin(tmp_path, monkeypatch, capsys):
    archive = tgz({"carillon": b"bin", "carillon.exe": b"exe"})
    pin(monkeypatch, archive, name="carillon")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    serve(monkeypatch, archive)
    assert main(["install-tool", "carillon"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert Path(out["installed"]).parent == tmp_path / ".local" / "bin"
    assert out["version"] == "v2.2.1"


def test_install_tool_command_reports_a_bad_checksum(tmp_path, monkeypatch, capsys):
    pin(monkeypatch, tgz({"himalaya": b"bin"}))
    serve(monkeypatch, b"x")
    assert main(["install-tool", "himalaya", "--dir", str(tmp_path / "bin")]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("mx: ") and "checksum" in captured.err
    assert files(tmp_path / "bin") == []


def test_install_tool_command_takes_only_the_pinned_tools(capsys):
    assert main(["install-tool", "curl"]) == 1
    assert "mx: " in capsys.readouterr().err


# --- app.toml ---


def test_app_toml_installs_through_mx():
    req = {r["bin"]: r for r in tomllib.loads(APP_TOML.read_text("utf-8"))["requires"]}
    assert req["himalaya"]["install"] == "uv run apps/mail/scripts/mx.py install-tool himalaya"
    assert set(req) == {"himalaya", "claude", "uv"}


def test_app_toml_requires_match_the_hub_rules():
    data = tomllib.loads(APP_TOML.read_text("utf-8"))
    reqs = {r["bin"]: r for r in data["requires"]}
    # himalaya's install line is `uv run ...`, so uv comes first
    assert list(reqs) == ["uv", "himalaya", "claude"]
    assert reqs["himalaya"]["name"] == "himalaya v2.2.1"
    assert set(reqs["claude"]["install"]) == {"linux", "macos", "windows"}
    assert "claude.ai/install" in reqs["claude"]["install"]["linux"]
    ledger = tomllib.loads((ROOT / "ledger" / "app.toml").read_text("utf-8"))
    ledger_uv = next(r for r in ledger["requires"] if r["bin"] == "uv")
    assert reqs["uv"] == ledger_uv  # the same uv table as the ledger's
    # carillon and ortie are installed when needed, not by every install of the app
    assert not {"carillon", "ortie"} & set(reqs)

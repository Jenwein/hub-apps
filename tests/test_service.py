import plistlib
import sys
from pathlib import Path, PurePosixPath

import pytest
from ledgerkit.cli import main
from ledgerkit.service import fava_unit
from ledgerkit.settings import LedgerError

BEAN = "main.beancount"


def test_systemd_unit():
    u = fava_unit("linux", "/home/u/.local/bin/fava",
                  Path("/home/u/.cc-hub/ledger/main.beancount"), 5000)
    assert ('ExecStart="/home/u/.local/bin/fava" -H 127.0.0.1 -p 5000 '
            '"/home/u/.cc-hub/ledger/main.beancount"') in u
    assert "Restart=on-failure" in u and "WantedBy=default.target" in u


def test_systemd_unit_sections():
    u = fava_unit("linux", "/usr/bin/fava", Path("/srv/ledger/main.beancount"), 5000)
    assert u.startswith("[Unit]\nDescription=Fava for the cc-hub ledger\n\n[Service]\n")
    assert u.endswith("\n[Install]\nWantedBy=default.target\n")


def test_launchd_plist():
    p = plistlib.loads(fava_unit(
        "macos", "/opt/fava", Path("/Users/u/hub/ledger/main.beancount"), 5001).encode())
    assert p["Label"] == "io.cc-hub.ledger.fava"
    assert p["ProgramArguments"] == [
        "/opt/fava", "-H", "127.0.0.1", "-p", "5001", "/Users/u/hub/ledger/main.beancount"]
    assert p["KeepAlive"] is True and p["RunAtLoad"] is True


def test_windows_is_on_demand():
    with pytest.raises(LedgerError, match="on demand"):
        fava_unit("windows", "fava.exe", Path("C:/hub/ledger/main.beancount"), 5000)


def test_paths_with_spaces_and_chinese_are_quoted():
    u = fava_unit("linux", "/home/u/my tools/fava",
                  Path("/home/u/交易 账本/main.beancount"), 5000)
    assert ('ExecStart="/home/u/my tools/fava" -H 127.0.0.1 -p 5000 '
            '"/home/u/交易 账本/main.beancount"') in u


def test_macos_arguments_keep_spaces_and_chinese():
    p = plistlib.loads(fava_unit(
        "macos", "/opt/my fava", Path("/Users/u/交易 账本/main.beancount"), 5001).encode())
    assert p["ProgramArguments"][0] == "/opt/my fava"
    assert p["ProgramArguments"][-1] == "/Users/u/交易 账本/main.beancount"


@pytest.mark.parametrize("bad", [
    '/home/u/a"b/main.beancount',   # systemd ends the quoted word at the double quote
    "/home/u/a\\b/main.beancount",  # systemd reads a backslash as an escape
    "/home/u/100%/main.beancount",  # systemd expands %h and friends in ExecStart
    "/home/u/a\nb/main.beancount",  # a line break ends the directive
])
def test_linux_refuses_paths_that_break_the_unit(bad):
    # PurePosixPath keeps a literal backslash, which a Windows Path would turn into a separator.
    with pytest.raises(LedgerError, match="cannot contain"):
        fava_unit("linux", "/usr/bin/fava", PurePosixPath(bad), 5000)


def test_linux_refuses_a_fava_path_that_breaks_the_unit():
    with pytest.raises(LedgerError, match="cannot contain"):
        fava_unit("linux", '/opt/fa"va', Path("/srv/ledger/main.beancount"), 5000)


# command line

@pytest.mark.skipif(sys.platform == "win32", reason="a systemd unit needs POSIX paths")
def test_cli_linux_unit(make_ledger, capsys):
    d = make_ledger()
    code = main(["fava-unit", "--platform", "linux", "--ledger", str(d / BEAN),
                 "--fava", "/opt/fava/bin/fava"])
    printed = capsys.readouterr()
    assert (code, printed.err) == (0, "")
    assert (f'ExecStart="/opt/fava/bin/fava" -H 127.0.0.1 -p 5000 '
            f'"{(d / BEAN).as_posix()}"') in printed.out
    assert printed.out.endswith("WantedBy=default.target\n")


def test_cli_macos_port_comes_from_config(make_ledger, capsys):
    d = make_ledger()
    config = d / "config.toml"
    config.write_text(config.read_text("utf-8").replace("port = 5000", "port = 5123"),
                      "utf-8", newline="\n")
    code = main(["fava-unit", "--platform", "macos", "--ledger", str(d / BEAN),
                 "--fava", "/opt/fava"])
    printed = capsys.readouterr()
    assert (code, printed.err) == (0, "")
    assert plistlib.loads(printed.out.encode())["ProgramArguments"] == [
        "/opt/fava", "-H", "127.0.0.1", "-p", "5123", (d / BEAN).as_posix()]


def test_cli_port_flag_needs_no_config(tmp_path, capsys):
    code = main(["fava-unit", "--platform", "macos", "--ledger", str(tmp_path / BEAN),
                 "--fava", "/opt/fava", "--port", "5001"])
    printed = capsys.readouterr()
    assert (code, printed.err) == (0, "")
    assert plistlib.loads(printed.out.encode())["ProgramArguments"][4] == "5001"


def test_cli_fava_defaults_to_the_path(make_ledger, monkeypatch, capsys):
    d = make_ledger()
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/local/bin/fava" if name == "fava" else None)
    code = main(["fava-unit", "--platform", "macos", "--ledger", str(d / BEAN)])
    printed = capsys.readouterr()
    assert (code, printed.err) == (0, "")
    assert plistlib.loads(printed.out.encode())["ProgramArguments"][0] == "/usr/local/bin/fava"


def test_cli_missing_fava_exit_1(make_ledger, monkeypatch, capsys):
    d = make_ledger()
    monkeypatch.setattr("shutil.which", lambda name: None)
    code = main(["fava-unit", "--platform", "linux", "--ledger", str(d / BEAN)])
    printed = capsys.readouterr()
    assert (code, printed.out) == (1, "")
    assert printed.err == "lk: fava is not on PATH\n"


def test_cli_relative_ledger_exit_1(capsys):
    code = main(["fava-unit", "--platform", "linux", "--ledger", "ledger/main.beancount",
                 "--fava", "/opt/fava", "--port", "5000"])
    printed = capsys.readouterr()
    assert (code, printed.out) == (1, "")
    assert printed.err.startswith("lk: ") and "absolute" in printed.err


def test_cli_no_config_and_no_port_exit_1(tmp_path, capsys):
    code = main(["fava-unit", "--platform", "macos", "--ledger", str(tmp_path / BEAN),
                 "--fava", "/opt/fava"])
    printed = capsys.readouterr()
    assert (code, printed.out) == (1, "")
    assert printed.err.startswith("lk: no config.toml")


def test_cli_unsupported_platform_exit_1(make_ledger, capsys):
    d = make_ledger()
    code = main(["fava-unit", "--platform", "windows", "--ledger", str(d / BEAN),
                 "--fava", "fava.exe"])
    printed = capsys.readouterr()
    assert (code, printed.out) == (1, "")
    assert printed.err == "lk: Fava runs on demand on windows; see fava.md\n"

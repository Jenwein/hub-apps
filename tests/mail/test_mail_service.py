"""The alert service (mailkit.service), the alert switch and the status (`mx watch-unit`,
`mx alerts on|off`, `mx alerts-status`).

The unit texts are generated and parsed here, never installed: no service, scheduled task or
launch agent is made. Paths of other platforms are given as pure paths, so the same tests run on
every operating system.
"""

import json
import os
import plistlib
import re
import runpy
import shutil
import socket
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath, PureWindowsPath

import pytest
from mailkit import clock as clock_module
from mailkit import service
from mailkit.alerts import AlertQueue
from mailkit.cli import main
from mailkit.context import load
from mailkit.health import Health
from mailkit.marks import Mark, load_marks, save_marks
from mailkit.paths import this_machine, write_json
from mailkit.settings import MailError, Settings
from mailkit.watch import check_here
from mailtest import ROOT

TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
HUB = PurePosixPath("/home/u/hub")
LOG = PurePosixPath("/home/u/.local/state/cc-hub-mail/x/watch.log")
ENV = {
    "PATH": "/home/u/.local/bin:/usr/bin",
    "HTTPS_PROXY": "http://127.0.0.1:7890",
    "ANTHROPIC_API_KEY": "sk-secret",
    "GH_TOKEN": "ghp_secret",
    "HOME": "/home/u",
}
MX = "/home/u/hub/apps/mail/scripts/mx.py"
UV = "/home/u/.local/bin/uv"
WIN_HUB = PureWindowsPath("C:/Users/u/hub")
WIN_LOG = PureWindowsPath("C:/Users/u/AppData/Local/cc-hub-mail/x/watch.log")
WIN_UV = r"C:\Users\u\.local\bin\uv.exe"
WIN_USER = r"PC\u"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def unit(platform="linux", uv=UV, hub=HUB, log=LOG, env=ENV, **more):
    return service.watch_unit(platform, uv, hub, log, env, **more)


def task(hub=WIN_HUB, uv=WIN_UV, user=WIN_USER) -> ET.Element:
    return ET.fromstring(unit("windows", uv, hub, WIN_LOG, ENV, user=user))


def found(root: ET.Element, path: str) -> ET.Element:
    element = root.find(path, TASK_NS)
    assert element is not None, path
    return element


def text_of(root: ET.Element, path: str) -> str:
    return found(root, path).text or ""


# --- the unit texts ----------------------------------------------------------------------------


def test_names():
    assert service.UNIT_NAME == "cc-hub-mail-watch.service"
    assert service.LAUNCHD_LABEL == "io.cc-hub.mail.watch"
    assert service.TASK_NAME == "cc-hub-mail-watch"


def test_linux_unit():
    text = unit()

    assert text.startswith("[Unit]\nDescription=cc-hub mail alerts\n\n[Service]\n")
    assert text.endswith("\n[Install]\nWantedBy=default.target\n")
    # not quoted: systemd reads a quoted WorkingDirectory as a relative path
    assert "\nWorkingDirectory=/home/u/hub\n" in text
    assert (
        f'ExecStart="{UV}" run "{MX}" --hub "/home/u/hub" watch\n') in text
    assert "Restart=on-failure\nRestartSec=30\n" in text
    assert 'Environment="PATH=/home/u/.local/bin:/usr/bin"\n' in text
    assert 'Environment="HTTPS_PROXY=http://127.0.0.1:7890"\n' in text


def test_linux_unit_keeps_spaces_and_chinese_in_paths():
    text = unit(uv="/home/u/my tools/uv", hub=PurePosixPath("/home/u/邮件 中心"))

    assert 'ExecStart="/home/u/my tools/uv" run "/home/u/邮件 中心/apps/mail/scripts/mx.py" ' in text
    assert '--hub "/home/u/邮件 中心" watch\n' in text


def test_only_kept_variables_reach_the_unit():
    env = {
        **ENV,
        "ALL_PROXY": "socks5://127.0.0.1:1080", "NO_PROXY": "localhost",
        "https_proxy": "http://lower", "CLAUDE_CONFIG_DIR": "/home/u/.claude-2",
        "CC_CONNECT_TOKEN": "tok", "OPENAI_API_KEY": "sk-other", "GITHUB_TOKEN": "ghp_x",
        "CC_HUB_MAIL_STATE": "/somewhere",
    }
    secrets = ("sk-secret", "ghp_secret", "tok", "sk-other", "ghp_x", "/somewhere", "HOME")

    for platform in ("linux", "macos", "windows"):
        text = unit(platform, env=env)
        for secret in secrets:
            assert secret not in text, (platform, secret)
        for name in ("ANTHROPIC_API_KEY", "GH_TOKEN", "CC_CONNECT_TOKEN", "OPENAI_API_KEY"):
            assert name not in text, (platform, name)

    linux = unit(env=env)
    kept = [line for line in linux.splitlines() if line.startswith("Environment=")]
    assert kept == [
        'Environment="PATH=/home/u/.local/bin:/usr/bin"',
        'Environment="HTTPS_PROXY=http://127.0.0.1:7890"',
        'Environment="ALL_PROXY=socks5://127.0.0.1:1080"',
        'Environment="NO_PROXY=localhost"',
        'Environment="https_proxy=http://lower"',
        'Environment="CLAUDE_CONFIG_DIR=/home/u/.claude-2"',
    ]
    assert service.ENV_KEEP == (
        "PATH", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "https_proxy", "http_proxy",
        "all_proxy", "no_proxy", "CLAUDE_CONFIG_DIR")


def test_an_unset_or_empty_variable_is_left_out():
    text = unit(env={"PATH": "/usr/bin", "HTTPS_PROXY": ""})

    assert 'Environment="PATH=/usr/bin"' in text
    assert "HTTPS_PROXY" not in text


@pytest.mark.parametrize("bad", ['a"b', "a\\b", "a%b", "a\nb", "a\rb"])
def test_unsafe_path_refused(bad):
    # A pure POSIX path keeps a literal backslash, which a Windows path would turn into a slash.
    with pytest.raises(MailError, match="cannot contain"):
        unit(hub=PurePosixPath(f"/home/u/{bad}"))
    with pytest.raises(MailError, match="cannot contain"):
        unit(uv=f"/home/u/{bad}/uv")


def test_a_dollar_sign_in_a_command_path_is_refused():
    # systemd expands ${NAME} in a command line, and $$ is one dollar sign
    with pytest.raises(MailError, match="cannot contain"):
        unit(hub=PurePosixPath("/home/u/${HOME}"))


@pytest.mark.parametrize("bad", ['a"b', "a\\b", "a\nb"])
def test_unsafe_value_of_a_kept_variable_refused(bad):
    with pytest.raises(MailError, match=r"PATH.*cannot contain"):
        unit(env={"PATH": f"/usr/bin:{bad}"})


def test_a_percent_sign_in_a_variable_is_written_as_two():
    # systemd reads %NAME in a setting as a specifier and %% as one percent sign; a proxy
    # address with a percent-encoded password is common
    env = {"HTTPS_PROXY": "http://u:p%40ss@proxy:8080", "NO_PROXY": "localhost"}

    assert 'Environment="HTTPS_PROXY=http://u:p%%40ss@proxy:8080"\n' in unit(env=env)
    assert 'Environment="NO_PROXY=localhost"\n' in unit(env=env)
    # launchd takes the value as it is
    plist = plistlib.loads(unit("macos", env=env).encode())
    assert plist["EnvironmentVariables"]["HTTPS_PROXY"] == "http://u:p%40ss@proxy:8080"


def test_a_percent_sign_in_a_path_is_still_refused():
    with pytest.raises(MailError, match="cannot contain"):
        unit(hub=PurePosixPath("/home/u/100%"))


def uv_run_environment(tmp_path, *rest: str):
    """What `uv run <script>` makes: a VIRTUAL_ENV in uv's cache and a PATH that starts with its
    scripts folder, followed by `rest`."""
    venv = tmp_path / "uv" / "cache" / "environments-v2" / "mx-1a2b3c"
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    return venv, scripts, {
        "VIRTUAL_ENV": str(venv), "PATH": os.pathsep.join([str(scripts), *rest])}


def kept_path(env) -> str | None:
    """The PATH that a launchd agent made from `env` has (None: no PATH)."""
    plist = plistlib.loads(unit("macos", env=env).encode())
    return plist["EnvironmentVariables"].get("PATH")


def test_the_path_of_a_unit_drops_the_uv_run_environment(tmp_path):
    venv, _scripts, env = uv_run_environment(tmp_path, "/opt/tools", "/usr/bin")
    expected = os.pathsep.join(["/opt/tools", "/usr/bin"])

    linux = unit(env=env)

    assert f'Environment="PATH={expected}"\n' in linux
    assert str(venv) not in linux and "VIRTUAL_ENV" not in linux  # it is not kept either
    assert kept_path(env) == expected


def test_the_path_keeps_a_folder_beside_the_environment_and_empty_entries(tmp_path):
    venv, scripts, env = uv_run_environment(tmp_path)
    beside = f"{venv}-other{os.sep}bin"
    env["PATH"] = os.pathsep.join([str(scripts), beside, str(venv), "", "/usr/bin"])

    assert kept_path(env) == os.pathsep.join([beside, "", "/usr/bin"])


def test_a_path_of_only_the_environment_is_left_out(tmp_path):
    _venv, _scripts, env = uv_run_environment(tmp_path)

    assert kept_path({**env, "HTTPS_PROXY": "http://p:1"}) is None
    assert 'Environment="HTTPS_PROXY=http://p:1"' in unit(env={**env, "HTTPS_PROXY": "http://p:1"})


def test_the_path_is_whole_when_no_environment_is_active():
    path = os.pathsep.join(["/opt/tools", "/usr/bin"])

    assert kept_path({"PATH": path}) == path
    assert kept_path({"PATH": path, "VIRTUAL_ENV": ""}) == path


@pytest.mark.skipif(os.name != "nt", reason="Windows paths differ in case only")
def test_the_environment_is_found_whatever_the_case_on_windows(tmp_path):
    _venv, scripts, env = uv_run_environment(tmp_path)
    env["PATH"] = os.pathsep.join([str(scripts).upper().replace("\\", "/"), "/usr/bin"])

    assert kept_path(env) == "/usr/bin"


@pytest.mark.skipif(os.name == "nt", reason="Windows paths differ in case only")
def test_the_environment_is_found_by_exact_case_elsewhere(tmp_path):
    _venv, scripts, env = uv_run_environment(tmp_path)
    shouted = str(scripts).upper()
    env["PATH"] = os.pathsep.join([shouted, "/usr/bin"])

    assert kept_path(env) == os.pathsep.join([shouted, "/usr/bin"])


def test_unknown_platform_refused():
    with pytest.raises(MailError, match="plan9"):
        unit("plan9")


def test_macos_plist_parses_and_restarts_on_failure_only():
    plist = plistlib.loads(unit("macos", log=PurePosixPath("/Users/u/state/watch.log")).encode())

    assert plist["Label"] == "io.cc-hub.mail.watch"
    assert plist["ProgramArguments"] == [UV, "run", MX, "--hub", "/home/u/hub", "watch"]
    assert plist["WorkingDirectory"] == "/home/u/hub"
    assert plist["EnvironmentVariables"] == {
        "PATH": "/home/u/.local/bin:/usr/bin", "HTTPS_PROXY": "http://127.0.0.1:7890"}
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["StandardOutPath"] == "/Users/u/state/watch.log"
    assert plist["StandardErrorPath"] == "/Users/u/state/watch.log"


def test_macos_plist_keeps_spaces_chinese_and_percent_signs_in_paths():
    # launchd takes an argv: nothing is quoted, so nothing needs refusing
    hub = PurePosixPath("/Users/u/邮件 100%")
    plist = plistlib.loads(unit("macos", uv="/opt/my uv", hub=hub).encode())

    assert plist["ProgramArguments"][0] == "/opt/my uv"
    assert plist["ProgramArguments"][2] == "/Users/u/邮件 100%/apps/mail/scripts/mx.py"
    assert plist["ProgramArguments"][4] == "/Users/u/邮件 100%"


def test_windows_xml_parses_and_has_logon_trigger_and_restart():
    root = task()

    assert root.tag == f"{{{TASK_NS['t']}}}Task"
    trigger = found(root, "t:Triggers/t:LogonTrigger")
    assert text_of(trigger, "t:Enabled") == "true"
    assert text_of(trigger, "t:UserId") == WIN_USER
    assert len(found(root, "t:Triggers")) == 1  # nothing else starts it

    principal = found(root, "t:Principals/t:Principal")
    assert text_of(principal, "t:UserId") == WIN_USER
    assert text_of(principal, "t:LogonType") == "InteractiveToken"  # only while logged on
    assert text_of(principal, "t:RunLevel") == "LeastPrivilege"

    restart = found(root, "t:Settings/t:RestartOnFailure")
    assert text_of(restart, "t:Interval") == "PT1M"
    assert text_of(restart, "t:Count") == "999"
    settings = found(root, "t:Settings")
    assert text_of(settings, "t:ExecutionTimeLimit") == "PT0S"
    assert text_of(settings, "t:DisallowStartIfOnBatteries") == "false"
    assert text_of(settings, "t:StopIfGoingOnBatteries") == "false"
    assert text_of(settings, "t:MultipleInstancesPolicy") == "IgnoreNew"
    assert text_of(settings, "t:Enabled") == "true"


def test_windows_task_runs_the_windowless_launcher_with_the_same_arguments():
    root = task()
    exe = found(root, "t:Actions/t:Exec")

    assert text_of(exe, "t:Command") == r"C:\Users\u\.local\bin\uvw.exe"  # beside uv.exe
    assert text_of(exe, "t:Arguments") == (
        r'run "C:\Users\u\hub\apps\mail\scripts\mx.py" --hub "C:\Users\u\hub" watch')
    assert text_of(exe, "t:WorkingDirectory") == r"C:\Users\u\hub"
    assert len(found(root, "t:Actions")) == 1


def test_windows_xml_stores_no_password_and_no_environment():
    text = unit("windows", WIN_UV, WIN_HUB, WIN_LOG, ENV, user=WIN_USER)

    assert "Password" not in text
    assert "sk-secret" not in text and "ghp_secret" not in text
    assert "Environment" not in text  # the task runs in the user's own logon environment
    assert text.startswith('<?xml version="1.0" encoding="UTF-16"?>\n<Task version="1.2" ')


def test_windows_xml_escapes_what_xml_reserves():
    hub = PureWindowsPath("C:/Users/R&D <team>/hub")
    root = task(hub=hub, user=r"AB&C\a<b")

    exe = found(root, "t:Actions/t:Exec")
    assert text_of(exe, "t:WorkingDirectory") == r"C:\Users\R&D <team>\hub"
    assert r'--hub "C:\Users\R&D <team>\hub"' in text_of(exe, "t:Arguments")
    assert text_of(root, "t:Triggers/t:LogonTrigger/t:UserId") == r"AB&C\a<b"


def test_windows_launcher_is_beside_uv_whatever_the_uv_name():
    root = task(uv=r"D:\tools with space\uv.exe")

    # a command with a space is quoted, as the Task Scheduler's own editor saves it
    assert text_of(root, "t:Actions/t:Exec/t:Command") == r'"D:\tools with space\uvw.exe"'


@pytest.mark.parametrize("bad", ['a"b', "a%b", "a\nb", "a\rb"])
def test_windows_unsafe_path_refused(bad):
    # a quote ends the argument, % is an environment variable for the task scheduler
    with pytest.raises(MailError, match="cannot contain"):
        task(hub=PureWindowsPath(f"C:/Users/{bad}/hub"))
    with pytest.raises(MailError, match="cannot contain"):
        task(uv=f"C:\\{bad}\\uv.exe")


def test_windows_path_ending_in_a_backslash_refused():
    # "C:\" would end the quoted argument at its last backslash and quote
    with pytest.raises(MailError, match="cannot end with a backslash"):
        task(hub=PureWindowsPath("C:/"))


def test_windows_user_defaults_to_the_current_one(monkeypatch):
    monkeypatch.setattr(service, "current_user", lambda: r"BOX\me")

    root = ET.fromstring(unit("windows", WIN_UV, WIN_HUB, WIN_LOG, ENV))

    assert text_of(root, "t:Triggers/t:LogonTrigger/t:UserId") == r"BOX\me"


@pytest.mark.parametrize(("domain", "expected"), [
    ("BOX", r"BOX\me"), (None, "me"), ("", "me")])
def test_current_user(monkeypatch, domain, expected):
    monkeypatch.delenv("USERDOMAIN", raising=False)
    monkeypatch.setattr("getpass.getuser", lambda: "me")
    if domain is not None:
        monkeypatch.setenv("USERDOMAIN", domain)

    assert service.current_user() == expected


# --- the command line --------------------------------------------------------------------------


CLI_UV = "/opt/tools/uv"


@pytest.fixture
def fake_uv(monkeypatch):
    """A uv at a POSIX-looking path and an environment of POSIX-looking paths without the
    variables of the machine the test runs on: a unit for Linux cannot carry the backslashes of a
    Windows PATH."""
    monkeypatch.setattr(service, "uv_path", lambda: CLI_UV)
    for name in service.ENV_KEEP:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", "/opt/tools:/usr/bin")
    return CLI_UV


def test_uv_path_is_what_the_path_finds_made_absolute(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "tools/uv")
    monkeypatch.chdir(tmp_path)

    assert service.uv_path() == str(tmp_path / "tools" / "uv")

    found = str(tmp_path / "bin" / "uv")
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: found)
    assert service.uv_path() == found


def test_cli_prints_the_unit_for_this_hub(hub, fake_uv, monkeypatch, capsys):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-do-not-print")
    root = hub.root.resolve().as_posix()

    assert main(["--hub", str(hub.root), "watch-unit", "--platform", "linux"]) == 0

    printed = capsys.readouterr()
    assert printed.err == ""
    assert f'ExecStart="{fake_uv}" run "{root}/apps/mail/scripts/mx.py" ' in printed.out
    assert f'--hub "{root}" watch\n' in printed.out
    assert 'Environment="PATH=/opt/tools:/usr/bin"' in printed.out
    assert 'Environment="HTTPS_PROXY=http://proxy:1"' in printed.out
    assert "sk-do-not-print" not in printed.out
    assert printed.out.endswith("WantedBy=default.target\n")  # printed as it is


def test_cli_macos_log_is_the_watch_log_of_the_state_directory(hub, fake_uv, capsys):
    assert main(["--hub", str(hub.root), "watch-unit", "--platform", "macos"]) == 0

    plist = plistlib.loads(capsys.readouterr().out.encode())
    log = (load(hub.root).state / "watch.log").as_posix()
    assert plist["StandardOutPath"] == log == plist["StandardErrorPath"]


def test_cli_out_writes_utf8_for_linux_and_macos(hub, fake_uv, tmp_path, capsys):
    target = tmp_path / "out" / "cc-hub-mail-watch.service"
    target.parent.mkdir()

    assert main([
        "--hub", str(hub.root), "watch-unit", "--platform", "linux", "--out", str(target)]) == 0

    assert capsys.readouterr().out == ""
    written = target.read_bytes()
    assert not written.startswith((b"\xff\xfe", b"\xef\xbb\xbf"))
    assert written.decode("utf-8").startswith("[Unit]\nDescription=cc-hub mail alerts\n")


def test_cli_out_writes_utf16_for_windows(hub, fake_uv, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(service, "current_user", lambda: r"PC\u")
    target = tmp_path / "task.xml"

    assert main([
        "--hub", str(hub.root), "watch-unit", "--platform", "windows", "--out", str(target)]) == 0

    assert capsys.readouterr().out == ""
    written = target.read_bytes()
    assert written.startswith(b"\xff\xfe")  # UTF-16 little endian with its byte order mark
    decoded = written.decode("utf-16")
    assert decoded.startswith('<?xml version="1.0" encoding="UTF-16"?>')
    root = ET.fromstring(written)  # the bytes, as schtasks reads them
    assert text_of(root, "t:Actions/t:Exec/t:Command").endswith("uvw.exe")
    assert text_of(root, "t:Actions/t:Exec/t:WorkingDirectory") == str(
        PureWindowsPath(hub.root.resolve().as_posix()))


def test_cli_out_into_a_missing_folder_is_an_error(hub, fake_uv, tmp_path, capsys):
    code = main([
        "--hub", str(hub.root), "watch-unit", "--platform", "linux", "--out",
        str(tmp_path / "missing" / "x.service")])

    assert code == 1
    assert capsys.readouterr().err.startswith("mx: cannot write ")


def test_cli_without_uv_is_an_error(hub, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)

    assert main(["--hub", str(hub.root), "watch-unit", "--platform", "linux"]) == 1

    assert capsys.readouterr().err.startswith("mx: uv is not on PATH")


def uv_folder(tmp_path, *, uvw: bool):
    folder = tmp_path / "uvdir"
    folder.mkdir()
    exe = folder / "uv.exe"
    exe.write_text("", "utf-8")
    if uvw:
        (folder / "uvw.exe").write_text("", "utf-8")
    return exe


def test_cli_windows_needs_uvw_beside_uv(hub, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(service, "current_user", lambda: r"PC\u")
    exe = uv_folder(tmp_path, uvw=False)
    monkeypatch.setattr(service, "uv_path", lambda: str(exe))

    assert main(["--hub", str(hub.root), "watch-unit", "--platform", "windows"]) == 1

    printed = capsys.readouterr()
    assert printed.out == ""
    assert printed.err.startswith("mx: uv has no uvw.exe next to it")
    assert "uv self update" in printed.err
    # a definition for another platform does not need it
    out = tmp_path / "agent.plist"
    assert main([
        "--hub", str(hub.root), "watch-unit", "--platform", "macos", "--out", str(out)]) == 0
    assert out.is_file()


def test_cli_windows_with_uvw_beside_uv_prints_the_task(hub, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(service, "current_user", lambda: r"PC\u")
    exe = uv_folder(tmp_path, uvw=True)
    monkeypatch.setattr(service, "uv_path", lambda: str(exe))

    assert main(["--hub", str(hub.root), "watch-unit", "--platform", "windows"]) == 0

    root = ET.fromstring(capsys.readouterr().out)
    assert text_of(root, "t:Actions/t:Exec/t:Command") == str(
        PureWindowsPath(str(exe)).with_name("uvw.exe"))


def test_cli_windows_does_not_check_a_uv_that_is_not_here(hub, fake_uv, monkeypatch, capsys):
    # the uv of the fixture is no file on this machine: the task is made for another one
    monkeypatch.setattr(service, "current_user", lambda: r"PC\u")

    assert main(["--hub", str(hub.root), "watch-unit", "--platform", "windows"]) == 0

    assert "uvw.exe" in capsys.readouterr().out


def test_cli_platform_is_required_and_checked(hub, capsys):
    assert main(["--hub", str(hub.root), "watch-unit"]) == 1
    assert "--platform" in capsys.readouterr().err
    assert main(["--hub", str(hub.root), "watch-unit", "--platform", "beos"]) == 1
    assert "beos" in capsys.readouterr().err


# --- the alert switch --------------------------------------------------------------------------

CONFIG = """\
# the settings of the mail app
[alerts]
enabled = false   # real-time alerts for urgent mail
host = ""                  # the machine that runs them: the one that runs cc-connect

[reader]
model = "sonnet"  # model of the tool-less reader (claude --model)

[chat]
language = "zh-CN"  # the user's language
"""


MACHINE_ID = "0a1b2c3d"
HOST = f"server-1#{MACHINE_ID}"  # this machine as the alert switch names it


@pytest.fixture
def named_host(hub, monkeypatch):
    """This machine is `server-1`, and its state directory holds the id MACHINE_ID."""
    monkeypatch.setattr(socket, "gethostname", lambda: "server-1")
    hub.state.mkdir(parents=True, exist_ok=True)
    (hub.state / "machine-id").write_text(MACHINE_ID + "\n", "utf-8")


def config_text(hub) -> str:
    return (hub.data / "config.toml").read_text("utf-8")


def test_alerts_on_off_edit_config_in_place(hub, named_host, capsys):
    (hub.data / "config.toml").write_text(CONFIG, "utf-8", newline="\n")

    assert main(["--hub", str(hub.root), "alerts", "on"]) == 0

    assert json.loads(capsys.readouterr().out) == {"enabled": True, "host": HOST}
    assert config_text(hub) == CONFIG.replace(
        "enabled = false   #", "enabled = true    #").replace('host = ""                  #',
                                                            f'host = "{HOST}" #')
    # comments and the other sections are still there, and the settings read back
    settings = load(hub.root).settings
    assert (settings.alerts_enabled, settings.alerts_host) == (True, HOST)
    assert (settings.reader_model, settings.language) == ("sonnet", "zh-CN")

    assert main(["--hub", str(hub.root), "alerts", "off"]) == 0

    assert json.loads(capsys.readouterr().out) == {"enabled": False, "host": ""}
    assert config_text(hub) == CONFIG  # the very text it was


def test_alerts_on_edits_the_seed_and_keeps_its_comments(hub, named_host, capsys):
    seed = config_text(hub)

    assert main(["--hub", str(hub.root), "alerts", "on"]) == 0

    lines = config_text(hub).splitlines()
    assert lines[:3] == [
        "[alerts]",
        "enabled = true    # real-time alerts for urgent mail",
        f'host = "{HOST}" # the machine that runs them: the one that runs cc-connect']
    assert config_text(hub).splitlines()[3:] == seed.splitlines()[3:]
    capsys.readouterr()


def test_alerts_on_twice_changes_nothing_more(hub, named_host, capsys):
    assert main(["--hub", str(hub.root), "alerts", "on"]) == 0
    once = config_text(hub)
    assert main(["--hub", str(hub.root), "alerts", "on"]) == 0

    assert config_text(hub) == once
    capsys.readouterr()


def test_alerts_on_keeps_crlf_line_ends(hub, named_host, capsys):
    (hub.data / "config.toml").write_bytes(CONFIG.replace("\n", "\r\n").encode("utf-8"))

    assert main(["--hub", str(hub.root), "alerts", "on"]) == 0

    data = (hub.data / "config.toml").read_bytes().decode("utf-8")
    assert data.count("\r\n") == CONFIG.count("\n") and data.count("\n") == data.count("\r\n")
    assert "enabled = true" in data
    capsys.readouterr()


def test_alerts_on_adds_a_missing_key_or_section(hub, named_host, capsys):
    config = hub.data / "config.toml"
    config.write_text('[alerts]\n# off for now\nenabled = false\n\n[chat]\nlanguage = ""\n',
                      "utf-8", newline="\n")
    assert main(["--hub", str(hub.root), "alerts", "on"]) == 0
    assert config_text(hub) == (
        f'[alerts]\n# off for now\nenabled = true\nhost = "{HOST}"\n\n[chat]\nlanguage = ""\n')

    config.write_text('[reader]\nmodel = "haiku"', "utf-8", newline="\n")  # no final line end
    assert main(["--hub", str(hub.root), "alerts", "on"]) == 0
    assert config_text(hub) == (
        f'[reader]\nmodel = "haiku"\n\n[alerts]\nenabled = true\nhost = "{HOST}"\n')

    config.write_text("[alerts]\nenabled = true", "utf-8", newline="\n")
    assert main(["--hub", str(hub.root), "alerts", "on"]) == 0
    assert config_text(hub) == f'[alerts]\nenabled = true\nhost = "{HOST}"\n'
    capsys.readouterr()


def test_alerts_on_leaves_other_tables_with_the_same_key_names_alone(hub, named_host, capsys):
    (hub.data / "config.toml").write_text(
        '[reader]\nenabled = "no"\nhost = "elsewhere"\n[alerts]\nenabled = false\nhost = ""\n',
        "utf-8", newline="\n")

    assert main(["--hub", str(hub.root), "alerts", "on"]) == 0

    assert config_text(hub) == (
        '[reader]\nenabled = "no"\nhost = "elsewhere"\n[alerts]\nenabled = true\n'
        f'host = "{HOST}"\n')
    capsys.readouterr()


def test_a_host_with_a_hash_or_a_quote_survives_a_second_edit(hub, monkeypatch, capsys):
    monkeypatch.setattr(socket, "gethostname", lambda: 'we"ird#name')
    assert main(["--hub", str(hub.root), "alerts", "on"]) == 0
    assert load(hub.root).settings.alerts_host.startswith('we"ird#name#')

    assert main(["--hub", str(hub.root), "alerts", "off"]) == 0

    assert load(hub.root).settings == Settings(
        alerts_enabled=False, alerts_host="", reader_model="sonnet", language="")
    capsys.readouterr()


def test_a_config_it_cannot_edit_is_left_as_it_is(hub, named_host, capsys):
    config = hub.data / "config.toml"
    text = "alerts = { enabled = false, host = '' }\n"
    config.write_text(text, "utf-8", newline="\n")

    assert main(["--hub", str(hub.root), "alerts", "on"]) == 1

    assert "set enabled and host there by hand" in capsys.readouterr().err
    assert config.read_text("utf-8") == text


def test_alerts_action_is_on_or_off(hub, capsys):
    assert main(["--hub", str(hub.root), "alerts", "maybe"]) == 1
    assert "maybe" in capsys.readouterr().err
    assert main(["--hub", str(hub.root), "alerts"]) == 1
    capsys.readouterr()


@pytest.mark.skipif(sys.platform == "win32", reason="permission bits mean nothing here")
def test_alerts_on_keeps_the_mode_of_config(hub, named_host, capsys):
    config = hub.data / "config.toml"
    config.chmod(0o640)

    assert main(["--hub", str(hub.root), "alerts", "on"]) == 0

    assert config.stat().st_mode & 0o777 == 0o640
    capsys.readouterr()


MARK = Mark("1", 42, "2026-10-19T08:00:00+00:00")


def test_alerts_on_clears_the_catch_up_marks(hub, named_host, capsys):
    ctx = load(hub.root)
    save_marks(ctx, {"qq": MARK, "gmail": MARK})
    assert set(load_marks(ctx)) == {"qq", "gmail"}

    assert main(["--hub", str(hub.root), "alerts", "on"]) == 0

    # the watcher's first sweep finds no mark, so it sets a baseline instead of catching up on the
    # mail that came while alerts were off
    assert load_marks(ctx) == {}
    capsys.readouterr()


def test_alerts_on_without_marks_makes_none(hub, named_host, capsys):
    assert main(["--hub", str(hub.root), "alerts", "on"]) == 0

    assert load_marks(load(hub.root)) == {}
    capsys.readouterr()


def test_alerts_off_keeps_the_marks(hub, named_host, capsys):
    ctx = load(hub.root)
    save_marks(ctx, {"qq": MARK})

    assert main(["--hub", str(hub.root), "alerts", "off"]) == 0

    assert load_marks(ctx) == {"qq": MARK}
    capsys.readouterr()


def test_a_failed_edit_does_not_clear_the_marks(hub, named_host, capsys):
    ctx = load(hub.root)
    save_marks(ctx, {"qq": MARK})
    (hub.data / "config.toml").write_text("alerts = {}\n", "utf-8", newline="\n")

    assert main(["--hub", str(hub.root), "alerts", "on"]) == 1

    assert load_marks(ctx) == {"qq": MARK}
    capsys.readouterr()


def test_alerts_off_removes_the_heartbeat(hub, named_host, capsys):
    ctx = load(hub.root)
    beat = ctx.state / "watch.json"
    write_json(beat, {"pid": 1, "at": NOW.isoformat(), "carillon": "up", "last_sweep": None})

    assert main(["--hub", str(hub.root), "alerts", "off"]) == 0

    # a stale heartbeat would count the time alerts are off as downtime
    assert not beat.exists()
    assert main(["--hub", str(hub.root), "alerts", "off"]) == 0  # nothing to remove: still fine
    capsys.readouterr()


def test_alerts_on_keeps_the_heartbeat(hub, named_host, capsys):
    ctx = load(hub.root)
    beat = ctx.state / "watch.json"
    write_json(beat, {"pid": 1, "at": NOW.isoformat(), "carillon": "up", "last_sweep": None})

    assert main(["--hub", str(hub.root), "alerts", "on"]) == 0

    assert beat.is_file()
    capsys.readouterr()


# --- the status --------------------------------------------------------------------------------


@pytest.fixture
def now(monkeypatch):
    monkeypatch.setattr(clock_module, "now", lambda: NOW)
    return NOW


def beat(hub, age: timedelta, carillon: str = "up", sweep: str | None = None) -> None:
    load(hub.root)  # makes the state directory
    write_json(hub.state / "watch.json", {
        "pid": 4242, "at": (NOW - age).isoformat(), "carillon": carillon, "last_sweep": sweep})


def status(hub, capsys) -> dict:
    assert main(["--hub", str(hub.root), "alerts-status"]) == 0
    printed = capsys.readouterr()
    assert printed.err == ""
    return json.loads(printed.out)


def test_alerts_status_of_a_hub_that_never_ran_alerts(hub, named_host, now, capsys):
    assert status(hub, capsys) == {
        "enabled": False, "host": "", "this_host": HOST, "watching": False,
        "carillon": None, "last_sweep": None, "queued_alerts": 0, "problems": []}


def test_alerts_status_of_a_running_watcher(hub, named_host, now, capsys):
    main(["--hub", str(hub.root), "alerts", "on"])
    capsys.readouterr()
    ctx = load(hub.root)
    Health(ctx).sweep_done(NOW - timedelta(minutes=7))
    beat(hub, timedelta(seconds=40), carillon="up")
    AlertQueue(ctx).add("an alert", ["qq/1/9"], NOW - timedelta(minutes=1))
    AlertQueue(ctx).add("a notice", [], NOW)
    Health(ctx).mailbox_failed("qq", "auth", "refused", NOW - timedelta(hours=1))

    result = status(hub, capsys)

    assert result["enabled"] is True and result["host"] == HOST
    assert result["this_host"] == HOST and result["watching"] is True
    assert result["carillon"] == "up"
    assert result["last_sweep"] == (NOW - timedelta(minutes=7)).isoformat()
    assert result["queued_alerts"] == 2
    assert [(p["mailbox"], p["kind"], p["until"]) for p in result["problems"]] == [
        ("qq", "auth", None)]


def test_alerts_status_sees_a_stale_heartbeat(hub, named_host, now, capsys):
    main(["--hub", str(hub.root), "alerts", "on"])
    capsys.readouterr()
    beat(hub, timedelta(minutes=10), carillon="up")  # the file says up, but it is 10 minutes old

    result = status(hub, capsys)

    assert result["watching"] is False
    assert result["carillon"] is None  # what a dead watcher last said is no answer
    assert result["enabled"] is True


@pytest.mark.parametrize(("age", "watching"), [
    (timedelta(seconds=119), True), (timedelta(seconds=121), False), (timedelta(0), True),
    (timedelta(minutes=-1), True), (timedelta(hours=-1), False)])
def test_alerts_status_watching_means_a_beat_in_the_last_two_minutes(
        hub, named_host, now, capsys, age, watching):
    beat(hub, age)

    assert status(hub, capsys)["watching"] is watching


def test_alerts_status_carillon_down_is_reported(hub, named_host, now, capsys):
    beat(hub, timedelta(seconds=5), carillon="down")

    result = status(hub, capsys)

    assert (result["watching"], result["carillon"]) == (True, "down")


def test_alerts_status_survives_an_unreadable_heartbeat(hub, named_host, now, capsys):
    load(hub.root)
    (hub.state / "watch.json").write_text("{not json", "utf-8")

    result = status(hub, capsys)

    assert (result["watching"], result["carillon"]) == (False, None)


def test_alerts_status_lists_only_problems_that_go_on(hub, named_host, now, capsys):
    ctx = load(hub.root)
    health = Health(ctx)
    health.mailbox_failed("qq", "connect", "down", NOW - timedelta(hours=5))
    health.mailbox_ok("qq", NOW - timedelta(hours=4))
    health.mailbox_failed("gmail", "reader", "no answer", NOW - timedelta(minutes=20))

    result = status(hub, capsys)

    assert [(p["mailbox"], p["kind"]) for p in result["problems"]] == [("gmail", "reader")]


def test_alerts_status_on_another_machine(hub, now, monkeypatch, capsys):
    (hub.data / "config.toml").write_text(
        '[alerts]\nenabled = true\nhost = "office"\n', "utf-8", newline="\n")
    monkeypatch.setattr(socket, "gethostname", lambda: "laptop")

    result = status(hub, capsys)

    assert (result["enabled"], result["host"]) == (True, "office")
    assert re.fullmatch(r"laptop#[0-9a-f]{8}", result["this_host"])
    assert result["watching"] is False


# --- which machine this is ---------------------------------------------------------------------


def test_this_machine_is_the_hostname_and_an_id_kept_in_the_state(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "box")
    state = tmp_path / "state"
    state.mkdir()

    first = this_machine(state)

    assert re.fullmatch(r"box#[0-9a-f]{8}", first)
    assert (state / "machine-id").read_text("utf-8").strip() == first.split("#")[1]
    assert this_machine(state) == first  # kept: made on first use only
    if os.name == "posix":
        assert (state / "machine-id").stat().st_mode & 0o777 == 0o600


def test_an_unreadable_machine_id_is_made_again(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "box")
    state = tmp_path / "state"
    state.mkdir()
    (state / "machine-id").write_text("not an id\n", "utf-8")

    found = this_machine(state)

    assert re.fullmatch(r"box#[0-9a-f]{8}", found)
    assert this_machine(state) == found


def test_two_state_directories_with_one_hostname_are_two_machines(hub, tmp_path, monkeypatch,
                                                                   capsys):
    # Windows and its WSL share the hostname, but each has a state directory of its own.
    monkeypatch.setattr(socket, "gethostname", lambda: "pc")
    assert main(["--hub", str(hub.root), "alerts", "on"]) == 0
    windows = json.loads(capsys.readouterr().out)["host"]
    assert re.fullmatch(r"pc#[0-9a-f]{8}", windows)
    assert status(hub, capsys)["this_host"] == windows

    monkeypatch.setenv("CC_HUB_MAIL_STATE", str(tmp_path / "wsl-state"))
    result = status(hub, capsys)

    assert result["host"] == windows
    assert re.fullmatch(r"pc#[0-9a-f]{8}", result["this_host"])
    assert result["this_host"] != windows
    with pytest.raises(MailError, match=f"alerts run on {windows}, not on this machine"):
        check_here(load(hub.root))

# --- mx.py without a console --------------------------------------------------------------------

MX_PY = ROOT / "mail" / "scripts" / "mx.py"


def run_mx_in_process(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", [str(MX_PY), *argv])
    with pytest.raises(SystemExit) as stop:
        runpy.run_path(str(MX_PY), run_name="__main__")
    return stop.value.code


def test_mx_starts_without_stdout_and_stderr(hub, monkeypatch):
    # under uvw.exe, the windowless launcher, python has neither stream
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    assert run_mx_in_process(monkeypatch, "--hub", str(hub.root), "chat-show") == 0
    assert run_mx_in_process(monkeypatch, "--hub", str(hub.root), "chat-set") == 1  # error: no chat


def pythonw() -> str | None:
    for name in ("pythonw.exe",):
        found_exe = os.path.join(os.path.dirname(sys.executable), name)
        if os.path.isfile(found_exe):
            return found_exe
    return None


@pytest.mark.skipif(
    sys.platform != "win32" or pythonw() is None, reason="needs pythonw, which has no streams")
def test_mx_runs_under_pythonw(hub):
    # the real launcher of a console-less python: its stdout and stderr are None. The run
    # switches alerts on, a change that shows in config.toml, as there is no output to read.
    env = {**os.environ, "CC_HUB_MAIL_STATE": str(hub.state), "PYTHONUTF8": "1"}
    run = subprocess.run(
        [pythonw(), str(MX_PY), "--hub", str(hub.root), "alerts", "on"],
        check=False, env=env, cwd=hub.root, timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)

    assert run.returncode == 0
    assert "enabled = true" in config_text(hub)

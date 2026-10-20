"""The alert service: the definition that runs `mx watch` in the background, the alert switch
(`mx alerts on|off`) and the status (`mx alerts-status`).

The definition is text, printed or written by `watch-unit`, never installed by the script: the
setup steps decide where it goes. Linux gets a systemd user unit, macOS a launchd agent and
Windows a Task Scheduler task. A service starts with a bare environment, and the watcher needs
`himalaya`, `carillon`, `claude` and `cc-connect` on `PATH` and the proxy the user's shell has,
so the unit carries the variables of ENV_KEEP and no other: no key or token of the shell lands in
a file. A Windows task takes no environment: it starts in the user's own logon environment, which
holds the `PATH` and the proxy the user set for the account.

The switch is the `[alerts]` table of `mail/config.toml`, edited line by line so that comments,
spacing, line ends and every other line stay as they were. `alerts on` also clears the catch-up
marks: the watcher's first sweep then sets a baseline instead of catching up on the mail that
came while alerts were off (the daily report covers that time). `alerts off` removes the
watcher's heartbeat, which a task that Windows ended by force leaves behind and which would count
the time alerts are off as downtime. Stop the service first: a watcher that still runs writes
its heartbeat again.
"""

from __future__ import annotations

import getpass
import json
import os
import plistlib
import re
import shutil
import socket
import tomllib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePath, PureWindowsPath
from xml.sax.saxutils import escape

from . import clock
from .alerts import AlertQueue
from .context import Ctx
from .health import HEARTBEAT, Health, heartbeat
from .marks import MARKS
from .paths import POSIX, locked, read_json, write_json, write_private
from .settings import MailError, load_settings

UNIT_NAME = "cc-hub-mail-watch.service"
LAUNCHD_LABEL = "io.cc-hub.mail.watch"
TASK_NAME = "cc-hub-mail-watch"
DESCRIPTION = "cc-hub mail alerts"
# What a unit gets from the environment of the command that makes it: the search path for the
# tools, the proxy of the shell, and the Claude Code configuration the reader runs with.
ENV_KEEP = (
    "PATH", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
    "https_proxy", "http_proxy", "all_proxy", "no_proxy", "CLAUDE_CONFIG_DIR")

# A heartbeat younger than this means the watcher runs (it writes one every few seconds).
WATCHING = timedelta(minutes=2)
CONFIG = "config.toml"  # in the hub's mail directory

# systemd misreads these inside a quoted word (quote, escape, specifier, line end).
UNSAFE_IN_UNIT = ('"', "\\", "%", "\n", "\r")
# It also expands ${NAME} in a command line, and reads $$ as one dollar sign.
UNSAFE_IN_COMMAND = (*UNSAFE_IN_UNIT, "$")
# The value of an Environment= setting is written with %% for each percent sign.
UNSAFE_IN_VALUE = ('"', "\\", "\n", "\r")
# Task Scheduler expands %NAME% in a command, its arguments and its folder, and a quote ends a
# quoted argument; XML allows no other control character.
UNSAFE_IN_TASK = re.compile(r'["%\x00-\x1f]')


def _checked(text: str, what: str, unsafe: tuple[str, ...] = UNSAFE_IN_UNIT, *,
             show: bool = True) -> str:
    """`text`, which a unit can carry as it is; a MailError when it cannot."""
    if any(char in text for char in unsafe):
        found = "a double quote, backslash, percent sign"
        found += ", dollar sign or line break" if "$" in unsafe else " or line break"
        shown = f": {text!r}" if show else ""  # a variable's value may hold a password
        raise MailError(f"{what} cannot contain {found}{shown}")
    return text


def _quoted(text: str, what: str, unsafe: tuple[str, ...] = UNSAFE_IN_UNIT, *,
            show: bool = True) -> str:
    return f'"{_checked(text, what, unsafe, show=show)}"'


def _task_text(text: str, what: str) -> str:
    if UNSAFE_IN_TASK.search(text):
        raise MailError(
            f"{what} cannot contain a double quote, percent sign or control character: {text!r}")
    return text


def _task_argument(text: str, what: str) -> str:
    _task_text(text, what)
    if text.endswith("\\"):  # the backslash would escape the closing quote
        raise MailError(f"{what} cannot end with a backslash: {text!r}")
    return f'"{text}"'


def current_user() -> str:
    """The account that runs the Windows task: `DOMAIN\\name`, or the name alone without a
    domain."""
    domain = os.environ.get("USERDOMAIN")
    name = getpass.getuser()
    return f"{domain}\\{name}" if domain else name


def _outside(path: str, folder: str) -> str:
    """The search path `path` without the entries that are `folder` or lie under it (compared
    the way this operating system compares paths: Windows ignores case and slash direction)."""
    root = os.path.normcase(os.path.normpath(folder))

    def inside(entry: str) -> bool:
        if not entry:
            return False
        where = os.path.normcase(os.path.normpath(entry))
        return where == root or where.startswith(root + os.sep)

    return os.pathsep.join(entry for entry in path.split(os.pathsep) if not inside(entry))


def _kept(env: Mapping[str, str]) -> dict[str, str]:
    """The variables of ENV_KEEP that `env` sets. The search path loses the entries under the
    active virtual environment: `uv run mx.py` puts the scripts folder of its throwaway
    environment first, which is gone once the command ends."""
    kept = {name: env[name] for name in ENV_KEEP if env.get(name)}
    venv = env.get("VIRTUAL_ENV")
    if venv and "PATH" in kept:
        kept["PATH"] = _outside(kept["PATH"], venv)
        if not kept["PATH"]:
            del kept["PATH"]
    return kept


def _environment(name: str, value: str) -> str:
    """An Environment= setting. systemd reads %NAME in a value as a specifier, so each percent
    sign is written twice."""
    text = _quoted(f"{name}={value}", f"the value of {name}", UNSAFE_IN_VALUE, show=False)
    return "Environment=" + text.replace("%", "%%")


def _linux_unit(uv: str, hub: PurePath, env: Mapping[str, str]) -> str:
    root = hub.as_posix()
    command = " ".join([
        _quoted(uv, "the uv path", UNSAFE_IN_COMMAND), "run",
        _quoted(f"{root}/apps/mail/scripts/mx.py", "the mx path", UNSAFE_IN_COMMAND),
        "--hub", _quoted(root, "the hub path", UNSAFE_IN_COMMAND), "watch"])
    return "\n".join([
        "[Unit]",
        f"Description={DESCRIPTION}",
        "",
        "[Service]",
        # not quoted: systemd takes the rest of the line as the path, and a quote would make it
        # a relative one
        f"WorkingDirectory={_checked(root, 'the hub path')}",
        *(_environment(name, value) for name, value in _kept(env).items()),
        f"ExecStart={command}",
        "Restart=on-failure",
        "RestartSec=30",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def _macos_plist(uv: str, hub: PurePath, log: PurePath, env: Mapping[str, str]) -> str:
    root = hub.as_posix()
    return plistlib.dumps({
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [uv, "run", f"{root}/apps/mail/scripts/mx.py", "--hub", root, "watch"],
        "WorkingDirectory": root,
        "EnvironmentVariables": _kept(env),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": log.as_posix(),
        "StandardErrorPath": log.as_posix(),
    }).decode("utf-8")


def _windows_task(uv: str, hub: PurePath, user: str) -> str:
    """The task: at the user's logon, `uvw.exe` (uv's launcher without a console, beside uv.exe,
    so no window opens on the desktop) runs the watcher. It runs only while the user is logged
    on, so no password is stored; it has no time limit and Task Scheduler starts it again once a
    minute when it fails."""
    root = PureWindowsPath(hub.as_posix())
    mx = root / "apps" / "mail" / "scripts" / "mx.py"
    launcher = _task_text(str(PureWindowsPath(uv).with_name("uvw.exe")), "the uv path")
    if " " in launcher:
        launcher = f'"{launcher}"'
    arguments = " ".join([
        "run", _task_argument(str(mx), "the mx path"), "--hub",
        _task_argument(str(root), "the hub path"), "watch"])
    who = escape(user)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <URI>\\{TASK_NAME}</URI>
    <Description>{DESCRIPTION}</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{who}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{who}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(launcher)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(_task_text(str(root), "the hub path"))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def watch_unit(platform: str, uv: str, hub: PurePath, log: PurePath, env: Mapping[str, str], *,
               user: str | None = None) -> str:
    """The service definition that runs `mx watch` for `hub` with the uv at `uv`: a systemd unit
    (`linux`), a launchd property list (`macos`, which appends the watcher's output to `log`) or
    Task Scheduler XML (`windows`, for `user`, default: the current account). Of `env` only the
    variables of ENV_KEEP are used. A path or value that the format cannot carry is a MailError."""
    if platform == "linux":
        return _linux_unit(uv, hub, env)
    if platform == "macos":
        return _macos_plist(uv, hub, log, env)
    if platform == "windows":
        return _windows_task(uv, hub, user if user is not None else current_user())
    raise MailError(f"no service definition for {platform}: use linux, macos or windows")


def uv_path() -> str:
    """The absolute path of uv, which the service runs mx through."""
    uv = shutil.which("uv")
    if uv is None:
        raise MailError("uv is not on PATH: the service runs mx through uv")
    return uv if os.path.isabs(uv) else os.path.abspath(uv)  # which() may return a relative one


def check_launcher(uv: str) -> None:
    """A Windows task starts `uvw.exe`, the launcher without a console that sits beside uv.exe:
    refuse a uv that is on this machine and has none. A uv that is not here (the task is made
    for another machine) cannot be checked."""
    exe = Path(uv)
    if exe.is_file() and not exe.with_name("uvw.exe").is_file():
        raise MailError(
            f"uv has no uvw.exe next to it ({exe.parent}): the scheduled task needs it, "
            "so that no window opens; update uv with uv self update")


def write_unit(path: Path, platform: str, text: str) -> None:
    """Write a definition to `path`: UTF-16 with a byte order mark for the Windows task, which
    `schtasks /XML` reads, UTF-8 otherwise."""
    data = ("﻿" + text).encode("utf-16-le") if platform == "windows" else text.encode("utf-8")
    try:
        write_private(Path(path), data)
    except OSError as err:
        raise MailError(f"cannot write {path}: {err}") from err


# --- the switch --------------------------------------------------------------------------------

_HEADER = re.compile(r"[ \t]*(\[\[?)[ \t]*([^\]\r\n]*?)[ \t]*\]\]?[ \t]*(?:#.*)?")
_VALUE = r"\"(?:[^\"\\\r\n]|\\.)*\"|'[^'\r\n]*'|[^\s#]+"
_ASSIGNMENT = re.compile(
    r"(?P<head>[ \t]*(?P<key>enabled|host)[ \t]*=[ \t]*)(?P<value>" + _VALUE + r")"
    r"(?P<gap>[ \t]*)(?P<comment>#[^\r\n]*)?(?P<eol>\r?\n)?")


def edit_alerts(text: str, enabled: bool, host: str) -> str:
    """`text`, a config.toml, with `enabled` and `host` of its `[alerts]` table set. Only those two
    values change: a comment after one stays, and stays in its column when it can; a key that is
    missing is added to the table, a table that is missing to the end of the file."""
    wanted = {"enabled": "true" if enabled else "false",
              "host": json.dumps(host, ensure_ascii=False)}  # a TOML basic string, too
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = re.findall(r"[^\n]*\n|[^\n]+", text)
    inside, last, seen = False, None, set()
    for index, line in enumerate(lines):
        header = _HEADER.fullmatch(line.rstrip("\r\n"))
        if header:
            inside = header[1] == "[" and header[2] == "alerts"
            last = index if inside else last
            continue
        if not inside:
            continue
        if line.strip() and not line.lstrip().startswith("#"):
            last = index
        found = _ASSIGNMENT.fullmatch(line)
        if found is None or found["key"] in seen:
            continue
        key, value, gap = found["key"], found["value"], found["gap"]
        seen.add(key)
        if value == wanted[key]:
            continue
        if found["comment"] and set(gap) <= {" "}:  # the comment stays in its column
            gap = " " * max(1, len(gap) + len(value) - len(wanted[key]))
        lines[index] = (
            f"{found['head']}{wanted[key]}{gap}{found['comment'] or ''}{found['eol'] or ''}")
    added = [f"{key} = {wanted[key]}{newline}" for key in wanted if key not in seen]
    if added and last is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += newline
        lines += ([newline] if lines else []) + [f"[alerts]{newline}", *added]
    elif added:
        if not lines[last].endswith("\n"):
            lines[last] += newline
        lines[last + 1:last + 1] = added
    return "".join(lines)


def _rewrite(path: Path, text: str) -> None:
    """Replace `path` by `text` (UTF-8) in one step, keeping its permissions."""
    try:
        mode = path.stat().st_mode & 0o777
        write_private(path, text.encode("utf-8"))
        if POSIX:
            path.chmod(mode)
    except OSError as err:
        raise MailError(f"cannot write {path}: {err}") from err


def clear_marks(ctx: Ctx) -> None:
    """Forget how far the watcher got in every inbox."""
    path = ctx.state / MARKS
    with locked(ctx.state):
        if path.exists():
            write_json(path, {})


def set_alerts(ctx: Ctx, enabled: bool) -> dict:
    """Turn alerts on for this machine (`enabled` and `host` = this machine's name; the marks are
    cleared) or off (`enabled` false, `host` empty; the heartbeat is removed). Returns the
    settings as they are now."""
    path = ctx.data / CONFIG
    host = socket.gethostname() if enabled else ""
    try:
        old = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise MailError(f"cannot read {path}: {err}") from err
    new = edit_alerts(old, enabled, host)
    try:  # the result must read back as asked: an edit that cannot be done is not half done
        alerts = tomllib.loads(new).get("alerts")
    except tomllib.TOMLDecodeError:
        alerts = None
    if not (isinstance(alerts, dict) and alerts.get("enabled") is enabled
            and alerts.get("host") == host):
        raise MailError(
            f"cannot edit the [alerts] lines of {path.name}: set enabled and host there by hand")
    if enabled:
        clear_marks(ctx)
    if new != old:
        _rewrite(path, new)
    if not enabled:
        try:
            (ctx.state / HEARTBEAT).unlink()
        except FileNotFoundError:
            pass
        except OSError as err:
            raise MailError(f"cannot remove {ctx.state / HEARTBEAT}: {err}") from err
    settings = load_settings(ctx.data)
    return {"enabled": settings.alerts_enabled, "host": settings.alerts_host}


# --- the status --------------------------------------------------------------------------------


def status(ctx: Ctx) -> dict:
    """How alerts are doing here. `watching` means the watcher wrote its heartbeat in the last
    WATCHING: an older heartbeat is a watcher that stopped, and says nothing about carillon, so
    `carillon` (`up` or `down`) is then None. `problems` are the mailbox problems that go on."""
    now = clock.now()
    beat = heartbeat(ctx.state)
    watching = beat is not None and abs(now - beat) < WATCHING
    carillon = None
    if watching:
        try:
            data = read_json(ctx.state / HEARTBEAT, None)
        except MailError:  # replaced right now
            data = None
        said = data.get("carillon") if isinstance(data, dict) else None
        carillon = said if said in ("up", "down") else None
    health = Health(ctx)
    sweep = health.last_sweep()
    everything = health.problems(datetime.min.replace(tzinfo=UTC), datetime.max.replace(tzinfo=UTC))
    return {
        "enabled": ctx.settings.alerts_enabled,
        "host": ctx.settings.alerts_host,
        "this_host": socket.gethostname(),
        "watching": watching,
        "carillon": carillon,
        "last_sweep": sweep.isoformat() if sweep else None,
        "queued_alerts": len(AlertQueue(ctx).pending()),
        "problems": [problem for problem in everything if problem["until"] is None],
    }

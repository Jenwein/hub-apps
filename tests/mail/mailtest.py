"""Helpers the mail tests import by name (the fixtures themselves are in conftest.py)."""

import re
import socket
import socketserver
import sys
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).parents[2]
SEED = ROOT / "mail" / "seed" / "mail"

# The mailbox that the greenmail_hub fixture sets up, and the password it stores for it.
GREENMAIL_BOX = "test"
GREENMAIL_SECRET = "x"


@dataclass(frozen=True)
class Hub:
    root: Path  # the hub; the working directory of the test
    data: Path  # root/mail, a copy of the seed mail directory
    state: Path  # the per-machine state directory (created by the first use)


def write_mailboxes(hub: Hub, text: str) -> None:
    (hub.data / "mailboxes.toml").write_text(text, "utf-8", newline="\n")


def write_placeholder_config(hub: Hub) -> None:
    """A himalaya.toml in the state directory that says nothing. Himalaya.run wants the file to
    exist (`mx configure` writes the real one); the fakes never read it. An existing one stays."""
    path = hub.state / "himalaya.toml"
    if not path.exists():
        hub.state.mkdir(parents=True, exist_ok=True)
        path.write_text("# a placeholder of the tests", "utf-8", newline="")


def windows_launcher(folder: Path, stubs: Path, name: str, script: Path) -> Path:
    """`<folder>/<name>.exe`: a launcher (distlib's, the one pip uses for its scripts) that runs
    `script` with sys.executable. The launcher hands its command line to Python as it came, so
    arguments such as `&XfJT0ZAB-`, `R%PATH%D` and `x^y` arrive exactly; stdin, stdout, stderr and
    the exit status pass through. The script runs as `__main__` with its own path as `__file__`
    and `sys.argv[0]`, as if it was started with `python script`."""
    from distlib.scripts import ScriptMaker, enquote_executable

    stubs.mkdir(exist_ok=True)
    stub = stubs / f"{name}.py"
    stub.write_text(
        f"#!python\nimport runpy\nrunpy.run_path({str(script)!r}, run_name='__main__')\n",
        "utf-8", newline="\n")
    maker = ScriptMaker(str(stubs), str(folder))
    maker.executable = enquote_executable(sys.executable)
    maker.force = True  # the stub is not "newer" than the script of the same name
    maker.clobber = True
    maker.make(stub.name)
    return folder / f"{name}.exe"


# GreenMail names itself with a Java InetAddress, `/172.17.0.2`, in the SMTP greeting and in the
# first line of the EHLO answer. himalaya's SMTP client refuses a server name that is no domain.
_SERVER_NAME = re.compile(rb"^(\d{3}[ -])\S*/\d{1,3}(?:\.\d{1,3}){3}")


def fix_server_name(line: bytes) -> bytes:
    """A reply line of an SMTP server with `/172.17.0.2` after the code made `localhost`."""
    return _SERVER_NAME.sub(rb"\1localhost", line)


def _close_write(sock: socket.socket) -> None:
    with suppress(OSError):
        sock.shutdown(socket.SHUT_WR)


def _copy(source: socket.socket, target: socket.socket) -> None:
    try:
        while data := source.recv(65536):
            target.sendall(data)
    except OSError:
        pass
    _close_write(target)


def _copy_replies(source: socket.socket, target: socket.socket) -> None:
    try:
        with source.makefile("rb") as lines:
            for line in lines:
                target.sendall(fix_server_name(line))
    except OSError:
        pass
    _close_write(target)


class _Forward(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        try:
            upstream = socket.create_connection(self.server.target, timeout=30)
        except OSError:
            return
        with upstream:
            upstream.settimeout(None)
            threading.Thread(target=_copy, args=(self.request, upstream), daemon=True).start()
            _copy_replies(upstream, self.request)


class SmtpProxy(socketserver.ThreadingTCPServer):
    """A forwarder on 127.0.0.1:`port` to the SMTP server `target` (host, port) that changes
    nothing but the server name in the replies (see fix_server_name)."""

    daemon_threads = True

    def __init__(self, target: tuple[str, int]):
        super().__init__(("127.0.0.1", 0), _Forward)
        self.target = target
        self.port = self.server_address[1]
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()

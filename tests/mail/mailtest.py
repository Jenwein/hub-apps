"""Helpers the mail tests import by name (the fixtures themselves are in conftest.py)."""

import sys
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

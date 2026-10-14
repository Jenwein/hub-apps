import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
APP = ROOT / "mail"


def manifest(app: Path) -> dict:
    return tomllib.loads((app / "app.toml").read_text("utf-8"))


def test_app_toml_fields():
    data = manifest(APP)
    app = data["app"]
    assert (app["name"], app["version"], app["min_hub"]) == ("mail", "0.1.0", "0.5.0")
    assert app["description"].strip()
    assert data["data"] == {"dirs": ["mail"]}
    assert [r["bin"] for r in data["requires"]] == ["uv"]


def test_requires_uv_the_way_ledger_does():
    def uv(app: Path) -> dict:
        return next(r for r in manifest(app)["requires"] if r["bin"] == "uv")

    assert uv(APP) == uv(ROOT / "ledger")
    assert set(uv(APP)["install"]) == {"linux", "macos", "windows"}


def test_seed_gitignore_keeps_state_and_downloads_out_of_git():
    lines = (APP / "seed/mail/.gitignore").read_text("utf-8").splitlines()
    assert {".state/", "attachments/"} <= {line.strip() for line in lines}


def test_mk_script_declares_what_it_needs():
    text = (APP / "scripts/mk.py").read_text("utf-8")
    block = re.search(r"^# /// script\n((?:#.*\n)+?)# ///$", text, re.MULTILINE)
    assert block, "mk.py needs a PEP 723 header"
    header = tomllib.loads("".join(line[2:] for line in block.group(1).splitlines(True)))
    assert header["requires-python"] == ">=3.11"
    assert header["dependencies"] == ["imap-tools>=1.15", "dnspython>=2.6"]


def test_app_paths_are_safe():
    files = [
        p.relative_to(APP).as_posix()
        for p in APP.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    ]
    assert not any("~" in f or f.split("/")[-1].endswith((".", " ")) for f in files)
    assert len({f.casefold() for f in files}) == len(files)
    assert not any(p.is_symlink() for p in APP.rglob("*"))

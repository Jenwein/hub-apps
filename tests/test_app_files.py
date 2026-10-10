import re
from pathlib import Path

from ledgerkit import cli

APP = Path(__file__).parents[1] / "ledger"


def parse_frontmatter(path: Path) -> tuple[dict[str, str], str]:
    """The way cc-hub reads a cron file: the block between the first two `---` lines."""
    match = re.match(
        r"^---\n(?P<head>.*?)\n---\n(?P<body>.*)$",
        path.read_text("utf-8").replace("\r\n", "\n"),
        re.DOTALL,
    )
    assert match, f"{path.name}: needs a frontmatter block between --- lines"
    head = {}
    for line in match.group("head").splitlines():
        key, sep, value = line.partition(":")
        if sep:
            head[key.strip()] = value.split("#", 1)[0].strip().strip('"')
    return head, match.group("body")


def test_cron_files():
    for name in ("prices", "monthly"):
        head, body = parse_frontmatter(APP / f"cron/{name}.md")
        assert head["default"] == "true" and head["session"] == "new_per_run"
        assert body.strip()
    assert parse_frontmatter(APP / "cron/prices.md")[0]["schedule"] == "0 18 * * 1-5"
    assert parse_frontmatter(APP / "cron/monthly.md")[0]["schedule"] == "0 9 1 * *"


def test_setup_and_remove_exist_and_name_the_service():
    assert "cc-hub-ledger-fava" in (APP / "SETUP.md").read_text("utf-8")
    assert "cc-hub-ledger-fava" in (APP / "REMOVE.md").read_text("utf-8")


def test_app_paths_are_safe():
    files = [
        p.relative_to(APP).as_posix()
        for p in APP.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    ]
    assert not any("~" in f or f.split("/")[-1].endswith((".", " ")) for f in files)
    assert len({f.casefold() for f in files}) == len(files)
    assert not any(p.is_symlink() for p in APP.rglob("*"))


def test_lk_commands_and_flags_named_in_setup_remove_and_cron_exist():
    commands = cli.build_parser()._subparsers._group_actions[0].choices
    texts = [APP / "SETUP.md", APP / "REMOVE.md", APP / "cron/prices.md", APP / "cron/monthly.md"]
    seen = set()
    for path in texts:
        # `lk.py <command> ...`, up to the end of the code span or line; a backslash continues.
        pattern = r"\blk\.py ([a-z][\w-]*)((?:[^`\n\\]|\\[\s\S])*)"
        for name, rest in re.findall(pattern, path.read_text("utf-8")):
            assert name in commands, f"{path.name}: lk has no command {name}"
            seen.add(name)
            known = {flag for action in commands[name]._actions for flag in action.option_strings}
            for flag in re.findall(r"(?<![\w-])--[a-z][\w-]*", rest):
                assert flag in known, f"{path.name}: lk {name} has no {flag}"
    assert {"fava-unit", "summary"} <= seen


def test_seed_prices_comment_names_the_prices_task():
    text = (APP / "seed/ledger/prices.beancount").read_text("utf-8")
    assert "monthly" not in text and "prices task" in text


def test_readme_shows_the_install_commands():
    text = (Path(__file__).parents[1] / "README.md").read_text("utf-8")
    assert "cc-hub tap add Jenwein/hub-apps" in text
    assert "cc-hub app install hub-apps/ledger" in text
    assert "HTTPS_PROXY" in text and "MIT" in text


def test_cron_bodies_survive_a_shell():
    # The hub-apps skill registers a task with `cc-connect cron add ... --prompt "<body>"`.
    # Inside double quotes a shell would run a backtick or $( ) and eat quotes and backslashes.
    forbidden = set("`$\"'\\")
    for name in ("prices", "monthly"):
        body = parse_frontmatter(APP / f"cron/{name}.md")[1]
        assert not forbidden & set(body), f"{name}.md: {sorted(forbidden & set(body))}"


def _setup_sections() -> dict[str, str]:
    text = (APP / "SETUP.md").read_text("utf-8")
    parts = re.split(r"^## ", text, flags=re.MULTILINE)[1:]
    return {part.split("\n", 1)[0]: part for part in parts}


def test_setup_asks_about_tailscale_in_step_1_and_step_3_only_acts():
    sections = _setup_sections()
    settings = next(v for k, v in sections.items() if k.startswith("1."))
    tailscale = next(v for k, v in sections.items() if k.startswith("3."))
    assert "[fava] tailscale" in settings
    assert "Ask " not in tailscale and "ask whether" not in tailscale.lower()


def test_setup_does_not_wait_for_linger():
    sections = _setup_sections()
    fava = next(v for k, v in sections.items() if k.startswith("2."))
    left = next(v for k, v in sections.items() if k.startswith("4."))
    assert "enable-linger" in fava and "enable-linger" not in left

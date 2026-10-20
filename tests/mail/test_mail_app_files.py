"""The files that make up the mail app, and the texts the agent follows to set it up and remove it:
the manifest, the report sections, SETUP.md, REMOVE.md and the README all agree with the code."""

import re
import shlex
import tomllib

from mailkit import cli, service
from mailtest import ROOT

APP = ROOT / "mail"
README = ROOT / "README.md"
SKILL = APP / "skills" / "mail"

# The commands only a tool calls; no text for the agent names them.
HIDDEN = {"secret-fetch", "token-store", "hook"}
# Flags of the programs SETUP.md and REMOVE.md tell the agent to run, besides mx.
FOREIGN_FLAGS = {
    "--user", "--now", "--no-pager", "--note", "--yes", "--porcelain", "--shutdown"}

FENCE = re.compile(r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```", re.DOTALL | re.MULTILINE)
SPAN = re.compile(r"`([^`]+)`")
# `mx.py <command> ...`, up to the end of the code span or line; a backslash continues the line.
INVOCATION = re.compile(r"\bmx\.py ([a-z][\w-]*)((?:[^`\n\\]|\\[\s\S])*)")
FLAG = re.compile(r"(?<![\w-])--[a-z][\w-]*")
STEP = re.compile(r"^## (\d+)\. (.+)$", re.MULTILINE)


def parse_frontmatter(path) -> tuple[dict[str, str], str]:
    """How cc-hub reads a cron or report file: the block between the first two `---` lines."""
    match = re.match(
        r"^---\n(?P<head>.*?)\n---\n(?P<body>.*)$",
        path.read_text("utf-8").replace("\r\n", "\n"),
        re.DOTALL,
    )
    assert match, f"{path.name}: needs a frontmatter block between --- lines"
    head = {}
    for line in match.group("head").splitlines():
        if line.strip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if sep:
            head[key.strip()] = value.split("#", 1)[0].strip().strip('"')
    return head, match.group("body")


def commands() -> dict:
    return cli.build_parser()._subparsers._group_actions[0].choices


def option_strings(parser) -> set[str]:
    return {flag for action in parser._actions for flag in action.option_strings}


def text(path) -> str:
    return path.read_text("utf-8")


def flat(path) -> str:
    return " ".join(text(path).split())


def steps(path) -> dict[int, str]:
    """The numbered `## N. Title` headings of SETUP.md."""
    return {int(number): title for number, title in STEP.findall(text(path))}


def code_texts(content: str) -> list[str]:
    """The fenced blocks and the inline code spans of `content`."""
    return [*FENCE.findall(content), *SPAN.findall(FENCE.sub("", content))]


def checked_texts() -> list:
    return [
        APP / "SETUP.md", APP / "REMOVE.md", APP / "reports/daily.md", APP / "reports/monthly.md",
        README,
    ]


def test_app_toml():
    manifest = tomllib.loads(text(APP / "app.toml"))
    assert manifest["app"]["name"] == "mail"
    assert manifest["app"]["version"] == "0.1.0"
    assert manifest["app"]["min_hub"] == "0.7.0"
    assert manifest["data"]["dirs"] == ["mail"]
    assert not (APP / "cron").exists(), "the app registers no scheduled tasks of its own"
    # carillon and ortie are installed when needed, not always
    assert {r["bin"] for r in manifest["requires"]} == {"uv", "himalaya", "claude"}


def test_report_sections():
    assert sorted(p.name for p in (APP / "reports").iterdir()) == ["daily.md", "monthly.md"]
    for period in ("daily", "monthly"):
        head, _ = parse_frontmatter(APP / f"reports/{period}.md")
        assert head == {"command": f"uv run apps/mail/scripts/mx.py report {period}", "title": "Mail"}


def test_setup_and_remove_exist_and_are_numbered_steps():
    assert steps(APP / "SETUP.md") and steps(APP / "REMOVE.md")
    assert list(steps(APP / "SETUP.md")) == [1, 2, 3, 4, 5, 6, 7]
    assert list(steps(APP / "REMOVE.md")) == [1, 2, 3, 4, 5]


def test_mx_commands_and_flags_in_setup_remove_reports_and_readme_exist():
    known = commands()
    seen: dict[str, set[str]] = {}
    for path in checked_texts():
        # the whole text: the report sections name their command in the frontmatter
        for name, rest in INVOCATION.findall(text(path)):
            assert name in known, f"{path.name}: mx has no command {name}"
            assert name not in HIDDEN, f"{path.name}: mx {name} is not for the agent"
            seen.setdefault(path.name, set()).add(name)
            allowed = option_strings(known[name])
            for flag in FLAG.findall(rest):
                assert flag in allowed, f"{path.name}: mx {name} has no {flag}"
    assert {
        "reader-check", "chat-show", "chat-set", "install-tool", "configure", "folders", "check",
        "list", "secret-set", "oauth-login", "alerts", "alerts-status", "watch-unit",
    } <= seen["SETUP.md"]
    assert {"alerts", "forget"} <= seen["REMOVE.md"]
    assert "report" in seen["daily.md"] and "report" in seen["monthly.md"]


def test_the_example_commands_parse():
    """Every full `uv run apps/mail/scripts/mx.py ...` line in SETUP.md and REMOVE.md is a command
    that mx accepts: required flags and choices included."""
    parser = cli.build_parser()
    found = 0
    for name in ("SETUP.md", "REMOVE.md"):
        for block in code_texts(text(APP / name)):
            for line in block.replace("\\\n", " ").splitlines():
                start = line.find("uv run apps/mail/scripts/mx.py ")
                if start < 0:
                    continue
                # up to a shell redirection or pipe
                words = shlex.split(re.split(r" [>|] ", line[start:])[0])
                parser.parse_args(words[3:])  # after `uv run apps/mail/scripts/mx.py`
                found += 1
    assert found >= 15


def test_mx_is_written_out_in_full():
    """A bare `mx <command>` is no command the agent can run."""
    for name in ("SETUP.md", "REMOVE.md"):
        content = text(APP / name)
        for command in commands():
            assert not re.search(rf"(?<![\w./-])mx {re.escape(command)}\b", content), (
                f"{name}: write mx {command} out in full")


def test_every_flag_in_setup_and_remove_belongs_to_some_command():
    known = set().union(*(option_strings(p) for p in commands().values()))
    known |= option_strings(cli.build_parser()) | FOREIGN_FLAGS
    for name in ("SETUP.md", "REMOVE.md"):
        for block in code_texts(text(APP / name)):
            for flag in FLAG.findall(block):
                assert flag in known, f"{name}: {flag} is no flag of mx or of a known program"


def test_setup_and_remove_name_the_service():
    names = (
        service.UNIT_NAME.removesuffix(".service"), service.LAUNCHD_LABEL, service.TASK_NAME)
    for path in ("SETUP.md", "REMOVE.md"):
        content = text(APP / path)
        for name in names:
            assert name in content, f"{path} never names {name}"
        assert f"~/.config/systemd/user/{service.UNIT_NAME}" in content, path
        assert f"~/Library/LaunchAgents/{service.LAUNCHD_LABEL}.plist" in content, path


def test_step_5_is_the_alerts_step():
    """The skill sends the agent to "step 5" of SETUP.md for the service."""
    assert "lerts" in steps(APP / "SETUP.md")[5]
    pattern = re.compile(r"SETUP\.md`?,? (?:step|steps) (\d+)|step (\d+) of `apps/mail/SETUP\.md`")
    found = []
    for path in sorted(SKILL.rglob("*.md")):
        for match in pattern.finditer(flat(path)):
            found.append(int(match[1] or match[2]))
    assert found, "the skill never points at a step of SETUP.md"
    assert set(found) == {5}


def test_apps_paths_named_in_the_texts_exist():
    """Every `apps/mail/<path>` that the skill, SETUP.md and REMOVE.md name is a file or folder of
    the app (the installed copy is `apps/mail`, the tap's is `mail`)."""
    pattern = re.compile(r"apps/mail/([\w./-]+)")
    found = set()
    for path in [*sorted(SKILL.rglob("*.md")), APP / "SETUP.md", APP / "REMOVE.md"]:
        for name in pattern.findall(text(path)):
            name = name.rstrip(".,")
            found.add(name)
            assert (APP / name).exists(), f"{path.name} names apps/mail/{name}"
    assert {"SETUP.md", "REMOVE.md", "scripts/mx.py", "rules/default.md"} <= found
    assert "skills/mail/providers.md" in found


def test_setup_and_remove_are_english_and_describe_the_design():
    """Prose is English; a localized word in a code span or block is data."""
    history = r"\b(?:R\d{1,3}|F-[A-Z]+|Task \d+|ruling|decision \d+)\b|§"
    for name in ("SETUP.md", "REMOVE.md"):
        content = text(APP / name)
        prose = SPAN.sub("", FENCE.sub("", content))
        assert not re.search(r"[　-鿿]", prose), name
        assert not re.search(history, content), name


def test_setup_follows_the_rules_of_the_skill():
    content = flat(APP / "SETUP.md")
    # nothing secret goes through the chat
    assert "never goes through the chat" in content
    # the private chat is recorded only from a direct chat
    assert "direct chat" in content
    # software is installed with the user's OK, the service is explained first
    assert "their OK" in content
    # what the user must do alone is handed over
    assert "cc-hub app setup-state mail waiting" in content
    # the Codex note
    assert "Codex" in content and "Claude Code" in content


def test_setup_service_steps():
    content = text(APP / "SETUP.md")
    flat_content = flat(APP / "SETUP.md")
    # per platform: the unit or definition, then its start
    assert "uv run apps/mail/scripts/mx.py watch-unit --platform linux" in content
    assert "uv run apps/mail/scripts/mx.py watch-unit --platform macos" in content
    assert "uv run apps/mail/scripts/mx.py watch-unit --platform windows --out" in content
    assert "systemctl --user daemon-reload" in content
    assert "systemctl --user enable --now cc-hub-mail-watch" in content
    assert "loginctl enable-linger" in content
    assert f"launchctl enable gui/$(id -u)/{service.LAUNCHD_LABEL}" in content
    assert "launchctl bootstrap" in content and "launchctl kickstart" in content
    assert "schtasks /Create /TN cc-hub-mail-watch /XML" in content
    assert "schtasks /Run /TN cc-hub-mail-watch" in content
    # a unit may hold proxy credentials
    assert "chmod 600" in content
    # a Windows task sees only the account's PATH
    assert "where himalaya" in content and "where carillon" in content
    assert "where claude" in content and "where cc-connect" in content
    # the switch is committed, and turning alerts on starts the catch-up again
    commit = 'git add -- mail/config.toml && git commit -m "mail: alerts on" -- mail/config.toml'
    assert commit in content
    assert "starting point" in flat_content
    # the check
    assert "watching" in flat_content and "two minutes" in flat_content


def test_remove_stops_the_service_before_alerts_are_switched_off():
    content = text(APP / "REMOVE.md")
    stop = {
        "linux": content.index("systemctl --user disable --now cc-hub-mail-watch"),
        "macos": content.index(f"launchctl bootout gui/$(id -u)/{service.LAUNCHD_LABEL}"),
        "windows": content.index("schtasks /End /TN cc-hub-mail-watch"),
    }
    assert content.index("schtasks /Delete /TN cc-hub-mail-watch /F") > stop["windows"]
    off = content.index("uv run apps/mail/scripts/mx.py alerts off")
    assert all(position < off for position in stop.values())
    commit = 'git add -- mail/config.toml && git commit -m "mail: alerts off" -- mail/config.toml'
    assert content.index(commit) > off
    # passwords and sign-ins are deleted only when the user says so
    assert "uv run apps/mail/scripts/mx.py forget --secrets" in content
    assert re.search(r"mx\.py forget(?! --secrets)", content), "forget without --secrets"
    assert content.index("mx.py forget") > off
    # mail/ stays
    assert "`mail/`" in content and "stays" in content


def test_app_paths_are_safe():
    files = [
        p.relative_to(APP).as_posix()
        for p in APP.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    ]
    assert not any("~" in f or f.split("/")[-1].endswith((".", " ")) for f in files)
    assert len({f.casefold() for f in files}) == len(files)
    assert not any(p.is_symlink() for p in APP.rglob("*"))


def test_readme_shows_the_install_command():
    content = text(README)
    assert "cc-hub app install hub-apps/mail" in content
    assert re.search(r"^\| \[`mail`\]\(mail/\) \|", content, re.MULTILINE)
    assert re.search(r"^## mail$", content, re.MULTILINE)
    # the ledger parts stay
    assert "cc-hub app install hub-apps/ledger" in content
    assert re.search(r"^\| \[`ledger`\]\(ledger/\) \|", content, re.MULTILINE)
    assert re.search(r"^## ledger$", content, re.MULTILINE)


def test_readme_mail_section_covers_what_the_brief_lists():
    content = text(README)
    section = content.split("\n## mail\n", 1)[1].split("\n## ", 1)[0]
    flat_section = " ".join(section.split())
    for word in ("Feishu", "QQ", "Gmail", "Outlook"):
        assert word in section, word
    for word in ("himalaya", "Claude Code", "uv", "carillon", "ortie"):
        assert word in section, word
    for phrase in (
            "confirmation code", "attachment", "daily", "monthly", "alerts",
            "without tools", "server terminal"):
        assert phrase in flat_section, phrase
    assert "cc-hub 0.7.0" in section

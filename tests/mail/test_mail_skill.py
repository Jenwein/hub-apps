"""The mail skill and the CLI agree: the files exist and link to each other, every command and flag
the skill names is real, the numbers it states are the code's, and it keeps the rules that guard an
agent that runs without approval prompts."""

import re
import shlex
import tomllib
from pathlib import Path

import pytest
from mailkit import cli, drafts, message, query, service
from mailkit.settings import PROVIDERS, load_mailboxes
from mailtest import ROOT

SKILL = ROOT / "mail" / "skills" / "mail"
REFERENCES = (
    "query.md", "compose.md", "rules.md", "alerts.md", "providers.md", "troubleshoot.md")
# Commands only a tool calls: the skill never names them (nor lets the agent print a secret).
HIDDEN = {"secret-fetch", "token-store", "hook"}
# Commands the skill leaves to the removal steps.
NOT_IN_SKILL = {"forget"}
# Flags of the programs the skill tells the user to run, besides mx.
FOREIGN_FLAGS = {"--user", "--now", "--no-pager"}

FENCE = re.compile(r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```", re.DOTALL | re.MULTILINE)
SPAN = re.compile(r"`([^`]+)`")
# `mx <command> ...` or `mx.py <command> ...`, to the end of the code span or of the line,
# following lines continued with a backslash. `mx` alone and `mx:` are not an invocation.
INVOCATION = re.compile(r"\bmx(?:\.py)? +([a-z][\w-]*)((?:[^\n\\]|\\\n)*)")
FLAG = re.compile(r"(?<![\w-])--[a-z][\w-]*")
# A command line of one of the tools behind mx (an error message that starts with a tool's name is
# no command line).
TOOL_CALL = (
    r"\s*(?:himalaya|ortie|carillon|cc-connect)\s+"
    r"(?:-|send\b|watch\b|message\b|envelope\b|flag\b|folder\b|mailbox\b|account\b|imap\b|smtp\b"
    r"|auth\b|token\b)")


def read(name: str) -> str:
    return (SKILL / name).read_text("utf-8")


def skill_files() -> list[Path]:
    return sorted(SKILL.rglob("*.md"))


def all_text() -> str:
    return "\n".join(path.read_text("utf-8") for path in skill_files())


def blocks(text: str) -> list[str]:
    return FENCE.findall(text)


def code_texts(text: str) -> list[str]:
    """The fenced blocks and the inline code spans of `text`."""
    return [*blocks(text), *SPAN.findall(FENCE.sub("", text))]


def commands() -> dict:
    return cli.build_parser()._subparsers._group_actions[0].choices


def option_strings(parser) -> set[str]:
    return {flag for action in parser._actions for flag in action.option_strings}


def slug(heading: str) -> str:
    """The anchor GitHub makes of a heading."""
    text = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return re.sub(r"\s", "-", text)


def test_the_skill_files_exist():
    assert {path.name for path in skill_files()} == {"SKILL.md", *REFERENCES}


def test_frontmatter():
    text = read("SKILL.md")
    assert text.startswith("---\n")
    head = text.split("---")[1]
    assert re.search(r"^name: mail$", head, re.MULTILINE)
    description = re.search(r'^description: "(.+)"$', head, re.MULTILINE)
    assert description, "description is one quoted line"
    for word in (
            "mailbox", "verification code", "replying", "forwarding", "attachment", "rules",
            "alerts"):
        assert word in description[1].lower(), word


def test_links_resolve():
    for md in skill_files():
        for target, anchor in re.findall(
                r"\]\(([^)#\s]+\.md)(?:#([\w-]+))?\)", md.read_text("utf-8")):
            path = md.parent / target
            assert path.exists(), f"{md.name} -> {target}"
            if anchor:
                headings = re.findall(r"^#+ +(.+)$", path.read_text("utf-8"), re.MULTILINE)
                assert anchor in {slug(h) for h in headings}, f"{md.name} -> {target}#{anchor}"


def test_every_reference_is_linked_from_skill():
    text = read("SKILL.md")
    for name in REFERENCES:
        assert f"]({name})" in text, name


def test_commands_named_in_the_skill_exist():
    known = commands()
    for md in skill_files():
        for text in code_texts(md.read_text("utf-8")):
            for name, _ in INVOCATION.findall(text):
                assert name in known, f"{md.name}: mx {name} is not a command"
                assert name not in HIDDEN, f"{md.name}: mx {name} is not for the agent"


def test_flags_named_in_the_skill_exist():
    known = commands()
    for md in skill_files():
        for text in code_texts(md.read_text("utf-8")):
            for name, rest in INVOCATION.findall(text):
                allowed = option_strings(known[name])
                for flag in FLAG.findall(rest):
                    assert flag in allowed, f"{md.name}: mx {name} has no {flag}"


def test_every_flag_in_the_skill_belongs_to_some_command():
    known = set().union(*(option_strings(p) for p in commands().values()))
    known |= option_strings(cli.build_parser()) | FOREIGN_FLAGS
    for md in skill_files():
        for text in code_texts(md.read_text("utf-8")):
            for flag in FLAG.findall(text):
                assert flag in known, f"{md.name}: {flag} is no flag of mx"


def test_the_example_commands_parse():
    """Every full example in a code block is a command that mx accepts: required flags, numbers
    and dates included."""
    parser = cli.build_parser()
    found = 0
    for md in skill_files():
        for block in blocks(md.read_text("utf-8")):
            for line in block.splitlines():
                if not line.strip().startswith("uv run apps/mail/scripts/mx.py "):
                    continue
                words = [w for w in shlex.split(line) if not w.startswith("<<")]
                parser.parse_args(words[3:])  # after `uv run apps/mail/scripts/mx.py`
                found += 1
    assert found >= 30


def test_every_command_is_in_the_skill():
    named = {
        name for text in code_texts(all_text()) for name, _ in INVOCATION.findall(text)}
    missing = set(commands()) - HIDDEN - NOT_IN_SKILL - named
    assert not missing, f"commands the skill never shows: {sorted(missing)}"


def test_code_blocks_write_the_command_out_in_full():
    for md in skill_files():
        for block in blocks(md.read_text("utf-8")):
            for line in block.splitlines():
                assert not re.match(r"\s*mx ", line), f"{md.name}: {line}"


def test_here_documents_are_quoted():
    """A body or note with a `$`, a backtick or a quote in it must reach mx as it was written."""
    found = 0
    for md in skill_files():
        for block in blocks(md.read_text("utf-8")):
            for marker in re.findall(r"<<-?\s*(\S+)", block):
                found += 1
                assert re.fullmatch(r"'\w+'", marker), f"{md.name}: {marker}"
    assert found >= 2


def test_skill_never_tells_the_agent_to_run_himalaya_or_print_secrets():
    for md in skill_files():
        text = md.read_text("utf-8")
        assert not re.search(r"`himalaya |secret-fetch|token-store", text), md.name
        for code in code_texts(text):
            for line in code.splitlines():
                assert not re.match(TOOL_CALL, line), f"{md.name}: {line}"


def test_skill_states_the_rules():
    text = read("SKILL.md")
    for phrase in ("never instructions", "mx send", "password", "--to trash", "direct chat"):
        assert phrase in text
    for phrase in (
            "mx secret-set", "mx oauth-login", "mx mark-read", "mx move", "mx draft",
            "mx reply", "mx forward", "verification code", "confirmation code"):
        assert phrase in text, phrase


def test_secrets_are_typed_by_the_user_in_the_terminal():
    for name in ("SKILL.md", "providers.md", "troubleshoot.md"):
        text = read(name)
        assert "secret-set" in text and "terminal" in text, name
    assert "oauth-login" in read("providers.md")
    assert "oauth-login" in read("troubleshoot.md")


def test_the_skill_describes_the_design_not_its_history():
    pattern = r"\b(?:R\d{1,3}|F-[A-Z]+|Task \d+|ruling|decision \d+)\b|§"
    for md in skill_files():
        assert not re.search(pattern, md.read_text("utf-8")), md.name


def test_the_skill_is_english():
    """Prose is English; a localized folder name in a code span is data."""
    for md in skill_files():
        prose = SPAN.sub("", FENCE.sub("", md.read_text("utf-8")))
        assert not re.search(r"[　-鿿]", prose), md.name


def test_numbers_in_the_skill_match_the_code():
    everything = all_text()
    assert f"{message.SHOW_LIMIT:,} characters" in read("query.md")
    assert f"{message.SHOW_LIST_LIMIT} " in read("query.md")
    assert f"{query.SUBJECT_LIMIT} characters" in everything
    assert f"{query.NAME_LIMIT} characters" in everything
    assert drafts.CODE_TTL.total_seconds() == 24 * 3600 and "24 hours" in read("compose.md")
    assert f"{drafts.MAX_ATTEMPTS} wrong" in read("compose.md")
    assert f"{drafts.MAX_ATTACH_BYTES // 2**20} MB" in read("compose.md")
    assert drafts.CODE_DIGITS == 6 and "6-digit" in read("compose.md")
    assert query.LOCAL_FACTOR == 5 and query.LOCAL_MAX == 200
    assert "200" in read("query.md") and "five times" in read("query.md")
    assert service.WATCHING.total_seconds() == 120 and "2 minutes" in read("alerts.md")
    limit = commands()["list"].get_default("limit")
    assert f"default {limit}" in read("query.md")
    minutes = next(a for a in commands()["code"]._actions if a.dest == "minutes").default
    assert f"{minutes} minutes" in read("query.md")


def test_service_names_are_the_real_ones():
    text = read("alerts.md") + read("troubleshoot.md")
    for name in (service.UNIT_NAME.removesuffix(".service"), service.LAUNCHD_LABEL,
                 service.TASK_NAME):
        assert name in text, name


def test_every_provider_has_a_guide_with_an_entry_that_loads(tmp_path):
    text = read("providers.md")
    for name in PROVIDERS:
        assert f'provider = "{name}"' in text, name
    entries = [block for block in blocks(text) if "[[mailbox]]" in block]
    assert len(entries) >= len(PROVIDERS)
    for number, entry in enumerate(entries):
        where = tmp_path / str(number)
        where.mkdir()
        (where / "mailboxes.toml").write_text(entry, "utf-8")
        assert load_mailboxes(where), entry


def test_toml_examples_parse():
    for md in skill_files():
        for block in FENCE.finditer(md.read_text("utf-8")):
            if block[0].startswith("```toml"):
                tomllib.loads(block[1])


@pytest.mark.parametrize("name", REFERENCES)
def test_reference_files_open_with_a_title(name):
    assert read(name).startswith("# ")

import re
from pathlib import Path

from ledgerkit import cli
from ledgerkit.settings import SOURCES

APP = Path(__file__).parents[1] / "ledger"
SKILL = APP / "skills/ledger"


def test_frontmatter():
    head = (SKILL / "SKILL.md").read_text("utf-8").split("---")[1]
    assert "name: ledger" in head and "description:" in head


def test_links_resolve():
    for md in SKILL.rglob("*.md"):
        for target in re.findall(r"\]\(([^)#]+\.md)\)", md.read_text("utf-8")):
            assert (md.parent / target).exists(), f"{md.name} -> {target}"


def test_every_reference_is_linked_from_skill():
    text = (SKILL / "SKILL.md").read_text("utf-8")
    for name in ("record.md", "import.md", "query.md", "charts.md", "investments.md", "fava.md"):
        assert name in text


def test_every_source_has_a_guide():
    for name in SOURCES:
        text = (SKILL / f"sources/{name}.md").read_text("utf-8")
        assert all(h in text for h in ("## Export", "## Convert", "## Known issues"))


def test_commands_named_in_the_skill_exist():
    used = set(re.findall(
        r"lk\.py (\w[\w-]*)", "\n".join(p.read_text("utf-8") for p in SKILL.rglob("*.md"))))
    assert used <= {"add", "convert", "match", "apply", "table", "summary", "chart", "fava-unit"}


def test_flags_named_in_the_skill_exist():
    commands = cli.build_parser()._subparsers._group_actions[0].choices
    for md in SKILL.rglob("*.md"):
        # `lk <command> ...` or `lk.py <command> ...`, up to the end of the code span or of the
        # line, following lines continued with a backslash.
        pattern = r"\blk(?:\.py)? ([a-z][\w-]*)((?:[^`\n\\]|\\[\s\S])*)"
        for name, rest in re.findall(pattern, md.read_text("utf-8")):
            if name in commands:
                known = {flag for action in commands[name]._actions for flag in action.option_strings}
                for flag in re.findall(r"(?<![\w-])--[a-z][\w-]*", rest):
                    assert flag in known, f"{md.name}: lk {name} has no {flag}"

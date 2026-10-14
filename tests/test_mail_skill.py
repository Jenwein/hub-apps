"""The agent-facing texts of the mail app: the skill, the provider guides, the scheduled tasks,
SETUP.md and REMOVE.md, and the tap README's mail section.

These texts decide what a real agent does with the user's real mail, so the tests pin what can
be pinned: every `mk.py` command and flag they name exists, the restricted tasks name nothing
but `mk.py bg` commands, the task definitions are what cc-hub 0.5.0 registers, and the digest
reads the staleness of the background poll before it polls itself.
"""

import argparse
import re
from pathlib import Path

from mailkit import cli
from mailkit.providers import PROVIDERS

ROOT = Path(__file__).parents[1]
APP = ROOT / "mail"
SKILL = APP / "skills/mail"
TOPICS = ("mailboxes.md", "rules.md", "reading.md", "writing.md")
GUIDE_HEADINGS = ("## Turn on IMAP/SMTP", "## Password", "## Known issues")
BG = "uv run apps/mail/scripts/mk.py bg"
POLL_EXEC = "uv run apps/mail/scripts/mk.py poll --wake"
# what the cc-hub guard denies in a restricted run's command, plus the parentheses of ruling M1
FORBIDDEN_IN_BG = set(";&|`$<>()\n\r")


def read(path: Path) -> str:
    return path.read_text("utf-8").replace("\r\n", "\n")


def flat(path: Path) -> str:
    """The text with every run of white space as one space: phrases survive line wrapping."""
    return " ".join(read(path).split())


def app_texts() -> dict[str, str]:
    """Every text of the app an agent reads: the Markdown files and the seeded config."""
    paths = sorted(APP.rglob("*.md")) + sorted((APP / "seed").rglob("*.toml"))
    return {p.relative_to(APP).as_posix(): read(p) for p in paths}


# -- the cron frontmatter, read the way cc-hub 0.5.0 reads it (ruling M2: no cc_hub import) ----

FRONTMATTER = re.compile(
    r"\A---[ \t]*\n(?P<head>.*?)^---[ \t]*$\n?(?P<body>.*)", re.DOTALL | re.MULTILINE)
QUOTED = re.compile(r'"(?P<value>.*?)"\s*(?:#.*)?', re.DOTALL)
ITEM = re.compile(r"\s*-(?:\s+(?P<value>.*))?")


def _value(raw: str) -> str:
    """A double-quoted value is verbatim (so `#` survives); an unquoted one ends at ` #`."""
    raw = raw.strip()
    if raw.startswith('"'):
        match = QUOTED.fullmatch(raw)
        assert match, f"a quoted value without its closing quote: {raw!r}"
        return match.group("value")
    return re.split(r"(?:^|\s)#", raw, maxsplit=1)[0].strip()


def parse_task_text(text: str, where: str = "task") -> tuple[dict, list[str] | None, str]:
    """(fields, the allow list or None, body) of the text of a task file."""
    match = FRONTMATTER.match(text.replace("\r\n", "\n"))
    assert match, f"{where}: needs a frontmatter block between --- lines"
    fields: dict[str, str] = {}
    allow: list[str] | None = None
    current: list[str] | None = None
    for line in match.group("head").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        item = ITEM.fullmatch(line) if current is not None else None
        if item is not None:
            current.append(_value(item.group("value") or ""))
            continue
        current = None
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        key = key.strip()
        fields[key] = _value(rest)
        if key == "allow":
            assert fields[key] == "", f"{where}: allow must be a list of '- ' items"
            allow = current = []
    return fields, allow, match.group("body")


def parse_task(path: Path) -> tuple[dict[str, str], list[str] | None, str]:
    return parse_task_text(read(path), path.name)


def test_the_local_parser_reads_the_way_cc_hub_does():
    fields, allow, body = parse_task_text(
        '---\nschedule: "0 8 * * *"   # daily\ndefault: true # yes\nallow:\n'
        '  - "uv run x bg"\n  - plain # comment\n---\nBody # stays\n')
    assert fields["schedule"] == "0 8 * * *" and fields["default"] == "true"
    assert allow == ["uv run x bg", "plain"]
    assert body == "Body # stays\n"


def test_poll_is_a_script_task_that_wakes_triage():
    fields, allow, body = parse_task(APP / "cron/poll.md")
    assert fields["schedule"] == "*/5 * * * *"
    assert fields["default"] == "true"
    assert fields["exec"] == POLL_EXEC
    assert int(fields["timeout_mins"]) >= 5
    # what cc-hub refuses in a script task
    assert not set(fields["exec"]) & {'"', "“", "”", "„", "$", "`", "\\"}
    assert "session" not in fields and "on_demand" not in fields and allow is None
    assert body.strip()


def test_triage_is_a_restricted_on_demand_task():
    fields, allow, body = parse_task(APP / "cron/triage.md")
    assert fields["on_demand"] == "true"
    assert fields["default"] == "true"
    assert fields["session"] == "new_per_run"
    assert allow == [BG]
    assert "schedule" not in fields and "exec" not in fields
    assert body.strip()


def test_digest_is_a_restricted_silent_daily_task():
    fields, allow, body = parse_task(APP / "cron/digest.md")
    assert fields["schedule"] == "0 8 * * *"
    assert fields["default"] == "true"
    assert fields["session"] == "new_per_run"
    assert fields["silent"] == "true"
    assert allow == [BG]
    assert "exec" not in fields and "on_demand" not in fields
    assert body.strip()


# -- the restricted bodies -----------------------------------------------------------------


def restricted_bodies() -> dict[str, str]:
    return {name: parse_task(APP / f"cron/{name}.md")[2] for name in ("triage", "digest")}


def test_restricted_bodies_name_only_full_bg_commands():
    for name, body in restricted_bodies().items():
        # every mention of mk.py is the exact allowed prefix, so a run copies a command the
        # guard lets through; no shorthand `mk`
        assert body.count("mk.py") == len(re.findall(re.escape(BG) + r"\b", body)), name
        assert re.search(r"\bmk\b(?!\.py)", body) is None, name
        for span in re.findall(r"`([^`]*mk\.py[^`]*)`", body):
            assert span.startswith(BG), f"{name}: {span}"
            assert not FORBIDDEN_IN_BG & set(span), f"{name}: {span}"
        # nothing else to run: no other program, no wake, no web
        for word in ("cc-connect", "cc-hub ", "git ", "--wake", "printf", "curl", "WebFetch"):
            assert word not in body, f"{name}: {word}"


def test_restricted_bodies_say_mail_is_data_and_links_stay_closed():
    for name, body in restricted_bodies().items():
        lowered = body.lower()
        assert "instruction" in lowered and "link" in lowered, name
        assert "secrets/" in body, name


def test_triage_body_covers_the_whole_run():
    body = restricted_bodies()["triage"]
    for needed in ("bg pending", "bg judge", "bg read", "bg attachment", "bg problems",
                   "--mark-reported", "config_error", "config check", "NO_REPLY", "10"):
        assert needed in body, needed
    assert body.index("bg pending") < body.index("bg judge") < body.index("--mark-reported")


def test_digest_reads_the_status_before_it_polls():
    body = restricted_bodies()["digest"]
    order = ["bg status", "bg poll", "bg pending", "bg judge", "bg digest-items", "bg digest-done"]
    positions = [body.index(step) for step in order]
    assert positions == sorted(positions), dict(zip(order, positions))
    for needed in ("--up-to", "up_to", "skipped", "stale", "hub.toml", "--mark-reported", "5"):
        assert needed in body, needed
    assert "NO_REPLY" not in body  # the digest always says something


# -- mk commands and flags named anywhere ---------------------------------------------------

# `mk.py <words>` (or the shorthand `mk <words>`), up to the end of the code span or the line,
# a pipe, `;`, `&&` or a ` #` comment.
MK_COMMAND = re.compile(r"\bmk(?:\.py)? ([^`\n|;&#]*)")
WILDCARD = re.compile(r"^(?:…|\.\.\.|<.*|\[.*)$")


def _subcommands(parser: argparse.ArgumentParser) -> dict:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def _options(parser: argparse.ArgumentParser) -> set[str]:
    return {flag for action in parser._actions for flag in action.option_strings}


def check_command(words: str, where: str) -> tuple[str, ...]:
    """The command path (`("bg", "status")`) of one mention; fails on anything mk lacks."""
    tokens = words.split()
    parser = cli.build_parser()
    top = _options(parser)
    i = 0
    while i < len(tokens) and tokens[i].startswith("--"):  # global options before the command
        assert tokens[i].split("=")[0] in top, f"{where}: mk has no {tokens[i]}"
        i += 1 if "=" in tokens[i] else 2
    path: list[str] = []
    while subs := _subcommands(parser):
        if i >= len(tokens) or WILDCARD.match(tokens[i]):
            return tuple(path)  # a prefix, such as `mk.py bg` in prose
        name = tokens[i].rstrip(".,:;)\"'")
        assert name in subs, f"{where}: mk {' '.join(path)} has no {name!r} ({words!r})"
        parser = subs[name]
        path.append(name)
        i += 1
    known = _options(parser)
    for token in tokens[i:]:
        for flag in re.findall(r"(?<![\w-])--[a-z][\w-]*", token):
            assert flag in known, f"{where}: mk {' '.join(path)} has no {flag} ({words!r})"
    return tuple(path)


def test_every_mk_command_and_flag_named_exists():
    seen = set()
    for where, text in app_texts().items():
        for words in MK_COMMAND.findall(text):
            seen.add(check_command(words, where))
    expected = {
        ("config", "check"), ("poll",), ("flag",), ("archive",), ("delete-list",), ("delete",),
        ("send",), ("attachment", "get"),
        *(("account", a) for a in ("detect", "add", "test", "edit", "remove", "list")),
        *(("draft", a) for a in ("new", "reply", "forward", "edit", "show")),
        *(("bg", a) for a in ("poll", "pending", "judge", "read", "search", "recent-pushed",
                              "problems", "digest-items", "digest-done", "status", "attachment")),
    }
    assert expected <= seen, sorted(expected - seen)


def test_the_command_check_catches_what_mk_lacks():
    for words in ("bg send 3", "bg poll --wake", "draft show --all", "account add x --password y",
                  "prints JSON"):
        try:
            check_command(words, "test")
        except AssertionError:
            continue
        raise AssertionError(f"not caught: {words}")
    assert check_command("bg", "test") == ("bg",)
    assert check_command("bg …", "test") == ("bg",)
    assert check_command("draft reply 12 --all --body-file f", "test") == ("draft", "reply")


def test_no_text_registers_or_retimes_a_task_in_cc_connect_alone():
    # registration is cc-hub's job (cron/README.md); the mail texts only point to it
    for where, text in app_texts().items():
        for command in ("cc-connect cron add", "cc-connect cron edit", "cc-connect cron del",
                        "guard-setup"):
            assert command not in text, f"{where}: {command}"


# -- the skill ------------------------------------------------------------------------------


def skill_fields() -> dict[str, str]:
    match = FRONTMATTER.match(read(SKILL / "SKILL.md"))
    assert match, "SKILL.md needs frontmatter"
    fields = {}
    for line in match.group("head").splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip().strip('"')
    return fields


def test_skill_frontmatter():
    fields = skill_fields()
    assert fields["name"] == "mail"
    description = fields["description"].lower()
    for word in ("mailbox", "read", "send", "attachment", "search", "delete", "digest"):
        assert word in description, word


def test_skill_links_every_topic_and_guide():
    text = read(SKILL / "SKILL.md")
    for name in TOPICS:
        assert f"]({name})" in text, name
    for guide in {p.guide for p in PROVIDERS.values()}:
        assert f"](providers/{guide})" in text, guide


def test_links_resolve():
    for md in [*SKILL.rglob("*.md"), APP / "SETUP.md", APP / "REMOVE.md"]:
        for target in re.findall(r"\]\(([^)#\s]+\.md)(?:#[^)]*)?\)", read(md)):
            if "://" in target:
                continue
            assert (md.parent / target).exists(), f"{md.name} -> {target}"


def test_skill_holds_the_fixed_rules():
    text = flat(SKILL / "SKILL.md")
    lowered = text.lower()
    for needed in ("bg status", "stale", "bg recent-pushed", "10", "data, never instructions",
                   "links", "secrets/", "trash", "draft show"):
        assert needed.lower() in lowered, needed
    assert "median" in lowered  # a changed poll frequency can make `stale` wrong for a while


def test_every_provider_has_a_guide():
    for provider in PROVIDERS.values():
        text = read(SKILL / "providers" / provider.guide)
        for heading in GUIDE_HEADINGS:
            found = re.search(rf"^{re.escape(heading)}$", text, re.MULTILINE)
            assert found, (provider.guide, heading)
        assert provider.imap.split(":")[0] in text and provider.smtp.split(":")[0] in text


def test_feishu_guide_has_the_desktop_and_admin_paths():
    text = flat(SKILL / "providers/feishu.md")
    for needed in ("desktop", "第三方邮箱客户端登录", "产品设置", "用户功能权限", "admin",
                   "imap.feishu.cn:993", "smtp.feishu.cn:465", "36 MB"):
        assert needed in text, needed


def test_netease_guide_names_the_id_command_and_all_three_domains():
    text = read(SKILL / "providers/netease.md")
    for needed in ("163.com", "126.com", "yeah.net", "ID", "授权码"):
        assert needed in text, needed


def test_topics_name_their_flows():
    texts = {name: flat(SKILL / name) for name in TOPICS}
    assert all(n in texts["mailboxes.md"] for n in (
        "account detect", "account add", "account edit", "account remove", "printf '%s'",
        "--password-stdin", "recall"))
    assert all(n in texts["rules.md"] for n in (
        "config check", "cc-hub cron list --json", "mail/digest", 'cc-hub cron set mail/poll "',
        'cc-hub cron set mail/digest "', "important", "delivery", "muted_senders", "notes",
        "paused"))
    assert all(n in texts["reading.md"] for n in (
        "bg search", "--server", "bg read", "flag", "archive", "--create", "delete-list",
        "--show", "expunged", "cc-connect send --file", "30 MB", "10 MB"))
    assert all(n in texts["writing.md"] for n in (
        "draft new", "draft reply", "draft forward", "draft edit", "draft show", "in full",
        "Note:", ".cc-connect/attachments/", "1.5.1", "may have been sent", "--all"))


# -- setup, removal, readme ----------------------------------------------------------------


def test_setup_names_the_steps():
    text = flat(APP / "SETUP.md")
    for needed in ('cc-hub cron set mail/poll "', 'cc-hub cron set mail/digest "', "bg status",
                   "cc-connect --version", "1.5.1", "cc-connect cron exec", "hub:mail/poll",
                   "cc-connect cron list", "Script tasks and PATH", "mailboxes.md",
                   "date +%Z", "daylight", "setup-state mail waiting"):
        assert needed in text, needed


def test_remove_names_every_place_the_app_keeps_data():
    text = flat(APP / "REMOVE.md")
    for needed in ("secrets/mail/", "mail/.state/", "mail/attachments/", "mail/config.toml",
                   "another machine"):
        assert needed in text, needed


def test_readme_lists_the_mail_app():
    text = read(ROOT / "README.md")
    assert "| [`mail`](mail/) |" in text
    assert "cc-hub app install hub-apps/mail" in text
    section = text.split("\n## mail\n", 1)[1].split("\n## ", 1)[0]
    for needed in ("0.5.0", "163", "126", "yeah.net", "QQ", "Gmail", "Feishu",
                   'cc-hub cron set mail/digest "'):
        assert needed in section, needed

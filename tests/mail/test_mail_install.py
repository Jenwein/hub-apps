"""The mail app installed the way a user installs it: from the tap's committed tree, through a
real cc-hub."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parents[2]
NEEDS_HUB = (0, 7, 0)  # app.toml says min_hub = "0.7.0"


def hub_version(env) -> tuple[int, ...] | None:
    """The version `cc-hub version` prints (cc-hub has no --version), or None when it cannot be
    read."""
    done = subprocess.run(
        ["cc-hub", "version"], check=False, env=env, capture_output=True, text=True,
        encoding="utf-8")
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", done.stdout) if done.returncode == 0 else None
    return tuple(int(part) for part in found.groups()) if found else None


@pytest.mark.cchub
def test_installs_from_the_tap(tmp_path):
    dirty = subprocess.run(
        ["git", "-C", str(REPO), "status", "--porcelain", "mail"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if dirty:
        pytest.skip("mail/ has uncommitted changes; the tap install reads the committed tree")

    hub = tmp_path / "hub"
    # HOME and USERPROFILE point into tmp_path, so nothing here can touch a real hub or config;
    # the mail state goes there too.
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "CC_HUB_DIR": str(hub),
        "CC_HUB_MAIL_STATE": str(tmp_path / "mail-state"),
        "PYTHONUTF8": "1",
    }
    version = hub_version(env)
    if version is None or version < NEEDS_HUB:
        pytest.skip(f"cc-hub {version or 'of unknown version'} is older than 0.7.0")

    def run(*args):
        done = subprocess.run(
            ["cc-hub", *args], check=False, env=env, capture_output=True, text=True,
            encoding="utf-8",
        )
        assert done.returncode == 0, (
            f"cc-hub {' '.join(args)} exited {done.returncode}\n"
            f"stdout:\n{done.stdout}\nstderr:\n{done.stderr}"
        )
        return done

    run("init", str(hub))
    run("tap", "add", str(REPO), "--name", "hub-apps")
    preview = run("app", "install", "hub-apps/mail").stdout
    assert "mail 0.1.0" in preview
    assert "setup steps: SETUP.md" in preview and "removal steps: REMOVE.md" in preview
    run("app", "install", "hub-apps/mail", "--yes")

    assert (hub / "apps/mail/app.toml").exists()
    assert (hub / "apps/mail/SETUP.md").exists() and (hub / "apps/mail/REMOVE.md").exists()
    assert (hub / "apps/mail/skills/mail/SKILL.md").exists()
    assert (hub / "mail/mailboxes.toml").exists()  # seeded
    assert (hub / "mail/config.toml").exists() and (hub / "mail/rules.md").exists()

    # The app is waiting for its setup, and cc-hub accepts its minimum version.
    listed = json.loads(run("app", "list", "--json").stdout)
    assert [(a["name"], a["setup"], a["min_hub_ok"]) for a in listed] == [("mail", "pending", True)]

    # Two report sections that run the app's own command, none approved yet; no scheduled task.
    sections = json.loads(run("report", "list", "--json").stdout)["sections"]
    assert [(s["app"], s["period"], s["kind"], s["approved"], s["error"]) for s in sections] == [
        ("mail", "daily", "command", False, None),
        ("mail", "monthly", "command", False, None),
    ]
    assert {s["command"] for s in sections} == {
        "uv run apps/mail/scripts/mx.py report daily",
        "uv run apps/mail/scripts/mx.py report monthly",
    }
    tasks = json.loads(run("cron", "list", "--json").stdout)
    assert [t["name"] for t in tasks if t["source"] == "mail"] == []

    # The installed copy runs from the hub root, as the texts say (the test's own Python has the
    # packages that uv would fetch).
    done = subprocess.run(
        [sys.executable, "apps/mail/scripts/mx.py", "chat-show"],
        cwd=hub, env=env, capture_output=True, text=True, encoding="utf-8", check=False)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"chat": None}

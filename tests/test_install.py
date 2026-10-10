import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.cchub
def test_installs_from_the_tap(tmp_path):
    """The whole app, installed the way a user installs it: from the tap's committed tree."""
    repo = Path(__file__).parents[1]
    dirty = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "ledger"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if dirty:
        pytest.skip("ledger/ has uncommitted changes; the tap install reads the committed tree")

    hub = tmp_path / "hub"
    # HOME and USERPROFILE point into tmp_path, so nothing here can touch a real hub or config.
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "CC_HUB_DIR": str(hub),
        "PYTHONUTF8": "1",
    }

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
    run("tap", "add", str(repo), "--name", "hub-apps")
    preview = run("app", "install", "hub-apps/ledger").stdout
    assert "ledger" in preview
    run("app", "install", "hub-apps/ledger", "--yes")
    assert (hub / "apps/ledger/app.toml").exists()
    assert (hub / "ledger/main.beancount").exists()  # seeded
    assert (hub / "ledger/.gitignore").read_text("utf-8").strip() == "inbox/"

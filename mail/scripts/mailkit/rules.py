"""The rules the reader sorts mail by: the app's default rules, then the user's own, which win.

The defaults are `rules/default.md` in the app, in English, and change with the app's updates. The
user's rules are `mail/rules.md` in the hub, in the user's own words and language; the agent edits
them when the user says how some mail should be sorted.
"""

from __future__ import annotations

from pathlib import Path

from .context import Ctx
from .settings import MailError

DEFAULT_FILE = Path("rules") / "default.md"  # in the app
USER_FILE = "rules.md"  # in the hub's mail directory


def _read(path: Path) -> str | None:
    """The text of `path` without a byte order mark and the white space around it; None when
    there is no such file."""
    try:
        return path.read_text("utf-8-sig").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as err:
        raise MailError(f"cannot read {path}: {err}") from err


def default_rules(ctx: Ctx) -> str:
    path = ctx.app / DEFAULT_FILE
    found = _read(path)
    if found is None:
        raise MailError(f"the app's default rules are missing: {path}")
    return found


def user_rules(ctx: Ctx) -> str:
    """The user's rules, "" when the hub has none."""
    return _read(ctx.data / USER_FILE) or ""


def combined(ctx: Ctx) -> str:
    """The rules as the reader gets them: the defaults first, then the user's, which win."""
    return (
        f"Default rules:\n\n{default_rules(ctx)}\n\n"
        "The user's own rules. Where they disagree with the default rules, the user's rules "
        f"win:\n\n{user_rules(ctx)}")

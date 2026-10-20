"""The time, from one place: tests patch `mailkit.clock.now`."""

from __future__ import annotations

from datetime import datetime


def now() -> datetime:
    """The current time, aware, in the local time zone."""
    return datetime.now().astimezone()

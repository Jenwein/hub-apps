"""The user's private chat: where free text from mail, drafts, codes and alerts are sent.

`mx chat-set` records the chat it runs in (the agent's `CC_SESSION_KEY` and `CC_PROJECT`) in
`chat.json`, and from then on everything goes there and nowhere else, with
`cc-connect send [-p <project>] -s <session> --stdin`: the text on stdin, never on the command line.
A text longer than one chat message (MAX_PART characters) is split at line breaks and sent in
parts, in order. `-s` is always given: without it cc-connect picks the first active session, which
may be a group chat.

A failure is a ChatError. Its message holds at most 200 characters of what cc-connect printed on
stderr and never the text that was sent, not even when cc-connect repeats it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from .context import Ctx
from .paths import read_json, write_json
from .query import clean
from .settings import MailError
from .tools import NO_WINDOW, kill_tree

MAX_PART = 4000  # characters in one chat message (cc-hub's limit)
SEND_TIMEOUT = 60  # seconds cc-connect has to take one part
DETAIL_LIMIT = 200  # characters of cc-connect's stderr that an error carries
ECHO_WINDOW = 12  # a run of this many characters that stderr shares with the text is an echo

CHAT_FILE = "chat.json"  # in the state directory
NO_CHAT = "no private chat recorded: run mx chat-set in the private chat"
LINE = re.compile(r"[^\n]*\n|[^\n]+")


@dataclass(frozen=True)
class Chat:
    project: str | None  # cc-connect's project, when the agent runs in one
    session: str  # cc-connect's session key of the chat


class ChatError(MailError):
    """The chat could not be reached: cc-connect is missing, failed or did not answer."""


# --- the recorded chat -------------------------------------------------------------------------


def record(ctx: Ctx, env: Mapping[str, str] = os.environ) -> Chat:
    """Record the chat of the agent that runs this: `CC_SESSION_KEY` (required) and `CC_PROJECT`
    (optional) of `env`. It replaces the chat recorded before."""
    session = env.get("CC_SESSION_KEY", "").strip()
    if not session:
        raise MailError(
            "run this from the agent in the user's private chat: CC_SESSION_KEY is not set")
    chat = Chat(env.get("CC_PROJECT", "").strip() or None, session)
    write_json(ctx.state / CHAT_FILE, asdict(chat))
    return chat


def saved(ctx: Ctx) -> Chat | None:
    """The recorded chat, None when there is none (or the file is not one `record` wrote)."""
    data = read_json(ctx.state / CHAT_FILE, None)
    if not isinstance(data, dict):
        return None
    session, project = data.get("session"), data.get("project")
    if not isinstance(session, str) or not session or not (
            project is None or isinstance(project, str)):
        return None
    return Chat(project or None, session)


def load(ctx: Ctx) -> Chat:
    """The recorded chat, else a MailError that says how to record one."""
    found = saved(ctx)
    if found is None:
        raise MailError(NO_CHAT)
    return found


# --- splitting ---------------------------------------------------------------------------------


def split(text: str, limit: int = MAX_PART) -> list[str]:
    """`text` in parts of at most `limit` characters that cut only at line breaks (a part ends
    with its `\\n`), so that joining the parts gives `text` back. A line longer than `limit` is
    cut hard. The parts are as full as the line breaks allow."""
    parts: list[str] = []
    current = ""
    for line in LINE.findall(text):
        if len(current) + len(line) <= limit:
            current += line
            continue
        if current:
            parts.append(current)
            current = ""
        while len(line) > limit:
            parts.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        parts.append(current)
    return parts


# --- sending -----------------------------------------------------------------------------------


def _echoes(message: str, text: str) -> bool:
    """Does `message` repeat a piece of `text`, ignoring white space? cc-connect's message could
    quote what it was given; an error must not carry mail."""
    message, text = " ".join(message[:2000].split()), " ".join(text.split())
    return any(
        message[at:at + ECHO_WINDOW] in text for at in range(len(message) - ECHO_WINDOW + 1))


def _detail(stderr: bytes, sent: str) -> str:
    """What cc-connect said, as one short line for an error message."""
    message = stderr.decode("utf-8", "replace")
    if _echoes(message, sent):
        return "its message repeated the text and is not shown"
    return clean(message, DETAIL_LIMIT)


def _send(chat: Chat, tail: list[str], data: bytes | None, where: str = "") -> None:
    """`cc-connect send [-p P] -s S <tail>` with `data` on stdin (none: stdin is closed). `where`
    says which part of a long text this is, for the error."""
    at = f" ({where})" if where else ""
    exe = shutil.which("cc-connect")
    if exe is None:
        raise ChatError("cc-connect is not on PATH: the private chat cannot be reached")
    argv = [exe, "send", *(["-p", chat.project] if chat.project else []), "-s", chat.session, *tail]
    extra = {"start_new_session": True} if os.name == "posix" else {}
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL if data is None else subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW, **extra)
    except OSError as err:
        raise ChatError(f"cannot run cc-connect: {err}") from err
    try:
        _, stderr = proc.communicate(data, timeout=SEND_TIMEOUT)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        raise ChatError(f"cc-connect did not answer within {SEND_TIMEOUT:g} s{at}") from None
    except BaseException:  # Ctrl+C: do not leave cc-connect running
        kill_tree(proc)
        raise
    if proc.returncode != 0:
        detail = _detail(stderr, data.decode("utf-8", "replace") if data else "")
        said = f": {detail}" if detail else ""
        raise ChatError(f"cc-connect exited with status {proc.returncode}{at}{said}")


def send_text(chat: Chat, text: str) -> None:
    """Send `text` to the chat, in parts of at most MAX_PART characters cut at line breaks, in
    order; a part with nothing but white space is left out. The first failure stops it (the
    parts before it have arrived)."""
    parts = [part for part in split(text) if part.strip()]
    for number, part in enumerate(parts, 1):
        where = f"part {number} of {len(parts)}" if len(parts) > 1 else ""
        _send(chat, ["--stdin"], part.encode("utf-8", "replace"), where)


def send_file(chat: Chat, path: Path) -> None:
    """Send the file at `path` to the chat."""
    _send(chat, ["--file", str(path)], None)

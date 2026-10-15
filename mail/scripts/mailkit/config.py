"""Settings of the mail app: config.toml in the mail directory.

Reading is tomllib plus a check of every value. config.toml belongs to the user as much as to the
agent, so a change to a `[mailboxes."<name>"]` section is a text edit of that section that leaves
every other byte alone, comments and line endings included (the way cc-hub edits hub.toml). An
edit whose result would not parse back to exactly the intended data is refused, not guessed at.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

CONFIG_FILE = "config.toml"

DEFAULT_IMPORTANT = (
    "Mail I must reply to or act on myself, and mail about money or account security "
    "(bills, charges, sign-in alerts)."
)
DEFAULT_DELIVERY = "instant"
DELIVERIES = ("instant", "digest")

SETTINGS_KEYS = ("important", "delivery", "muted_senders", "notes")
# what a mailbox section leaves out when it keeps the default
OPTIONAL_DEFAULTS = {
    "paused": False, "important": None, "delivery": None, "muted_senders": [], "notes": [],
}
# the keys of a mailbox section, in the order a new section writes them
MAILBOX_KEYS = (
    "address", "provider", "imap", "smtp", "added", "paused",
    "important", "delivery", "muted_senders", "notes",
)


class MailError(Exception):
    """An expected failure. The command line prints `mk: <message>` and exits with 1."""


@dataclass
class Mailbox:
    name: str
    address: str
    provider: str
    imap: str  # host:port
    smtp: str  # host:port
    added: str  # ISO-8601 with an offset
    paused: bool = False
    important: str | None = None  # None: the user-level value applies
    delivery: str | None = None  # None: the user-level value applies
    muted_senders: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Settings:
    important: str = DEFAULT_IMPORTANT
    delivery: str = DEFAULT_DELIVERY
    muted_senders: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    mailboxes: dict[str, Mailbox] = field(default_factory=dict)


# ---- reading ----

ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+$")
HOST_PORT = re.compile(r"^(?P<host>[^\s:\[\]]+|\[[0-9A-Fa-f:.]+\]):(?P<port>[0-9]{1,5})$")
UNUSABLE_NAME = re.compile(r'[\x00-\x1f\x7f"\\]')


def _check_name(name) -> None:
    if (
        not isinstance(name, str)
        or not name
        or name != name.strip()
        or UNUSABLE_NAME.search(name)
    ):
        raise MailError(
            f"{name!r} is not a usable mailbox name (not empty, no space at either end, "
            "no quotes, backslashes or control characters)"
        )


def _toml(text: str) -> dict:
    try:
        return tomllib.loads(text.removeprefix("\ufeff"))
    except tomllib.TOMLDecodeError as exc:
        raise MailError(f"config.toml is not valid TOML: {exc}") from None


def _boxes(data: dict) -> dict:
    boxes = data.get("mailboxes", {})
    if not isinstance(boxes, dict):
        raise MailError(
            'config.toml: mailboxes must hold one [mailboxes."<name>"] table per mailbox'
        )
    return boxes


def _only(table: dict, keys: tuple[str, ...], where: str) -> None:
    for key in table:
        if key not in keys:
            raise MailError(
                f"config.toml: {where} has {key!r}, which is not a setting here "
                f"(the settings are {', '.join(keys)})"
            )


def _text(table: dict, key: str, where: str) -> str | None:
    """The non-empty text under `key`, or None when there is no such key."""
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, str) or not value.strip():
        raise MailError(f"config.toml: {where} {key} must be non-empty text, got {value!r}")
    return value


def _need(table: dict, key: str, where: str) -> str:
    value = _text(table, key, where)
    if value is None:
        raise MailError(f"config.toml: {where} needs {key}")
    return value


def _delivery(table: dict, where: str) -> str | None:
    value = table.get("delivery")
    if value is not None and value not in DELIVERIES:
        raise MailError(
            f'config.toml: {where} delivery must be "instant" or "digest", got {value!r}'
        )
    return value


def _strings(table: dict, key: str, where: str) -> list[str]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise MailError(
            f"config.toml: {where} {key} must be a list of non-empty texts, got {value!r}"
        )
    return list(value)


def _host_port(table: dict, key: str, where: str) -> str:
    value = _need(table, key, where)
    match = HOST_PORT.match(value)
    if match is None or not 0 < int(match["port"]) < 65536:
        raise MailError(
            f'config.toml: {where} {key} must be host:port, like "imap.example.com:993", '
            f"got {value!r}"
        )
    return value


def _added(table: dict, where: str) -> str:
    value = table.get("added")
    if isinstance(value, datetime) and value.tzinfo is not None:  # written without quotes
        return value.isoformat()
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            return value
    raise MailError(
        f"config.toml: {where} added must be an ISO-8601 time with an offset, like "
        f'"2026-10-14T10:00:00+08:00", got {value!r}'
    )


def _mailbox(name: str, table) -> Mailbox:
    _check_name(name)
    where = f'[mailboxes."{name}"]'
    if not isinstance(table, dict):
        raise MailError(f"config.toml: {where} must be a table")
    _only(table, MAILBOX_KEYS, where)
    address = _need(table, "address", where)
    if not ADDRESS.match(address):
        raise MailError(f"config.toml: {where} address must be an email address, got {address!r}")
    paused = table.get("paused", False)
    if not isinstance(paused, bool):
        raise MailError(f"config.toml: {where} paused must be true or false, got {paused!r}")
    return Mailbox(
        name=name,
        address=address,
        provider=_need(table, "provider", where),
        imap=_host_port(table, "imap", where),
        smtp=_host_port(table, "smtp", where),
        added=_added(table, where),
        paused=paused,
        important=_text(table, "important", where),
        delivery=_delivery(table, where),
        muted_senders=_strings(table, "muted_senders", where),
        notes=_strings(table, "notes", where),
    )


def parse(text: str) -> Settings:
    """The settings in the text of a config.toml; MailError names the key that is wrong."""
    data = _toml(text)
    for key in data:
        if key not in ("settings", "mailboxes"):
            raise MailError(
                f'config.toml: {key!r} is not part of the settings; the file holds [settings] '
                'and [mailboxes."<name>"] sections'
            )
    table = data.get("settings", {})
    if not isinstance(table, dict):
        raise MailError("config.toml: settings must be a table")
    where = "[settings]"
    _only(table, SETTINGS_KEYS, where)
    return Settings(
        important=_text(table, "important", where) or DEFAULT_IMPORTANT,
        delivery=_delivery(table, where) or DEFAULT_DELIVERY,
        muted_senders=_strings(table, "muted_senders", where),
        notes=_strings(table, "notes", where),
        mailboxes={name: _mailbox(name, box) for name, box in _boxes(data).items()},
    )


def config_path(mail_dir: Path) -> Path:
    return Path(mail_dir) / CONFIG_FILE


def read_text(mail_dir: Path) -> str:
    """The text of config.toml exactly as stored: line endings are not translated."""
    path = config_path(mail_dir)
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        raise MailError(f"{path} not found") from None
    except UnicodeDecodeError as exc:
        raise MailError(f"{path} is not UTF-8 text: {exc}") from None
    except OSError as exc:
        raise MailError(f"cannot read {path}: {exc}") from None


def atomic_write(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """Replace `path` with `data` in one step: a reader sees the old file or the new one,
    never part of either. The bytes go to a temporary file next to `path`, then `os.replace`.

    `mode` is the POSIX permission of the new file (default: that of the file it replaces,
    else 0o644). The temporary file is created with that mode, so a secret is never readable by
    others, not even for a moment. OSError when it cannot be written; no temporary file is left.
    """
    path = Path(path)
    if mode is None:
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except FileNotFoundError:
            mode = 0o644
    temp = path.with_name(f".{path.name}.{os.urandom(6).hex()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(temp, flags, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(temp, mode)  # the umask may have taken bits the old file had
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


def write_text(mail_dir: Path, text: str) -> None:
    """Store `text` as config.toml exactly as it is, line endings included, in one step."""
    path = config_path(mail_dir)
    try:
        atomic_write(path, text.encode("utf-8"))
    except OSError as exc:
        raise MailError(f"cannot write {path}: {exc}") from None


def load(mail_dir: Path) -> Settings:
    return parse(read_text(mail_dir))


def _union(first: list[str], second: list[str]) -> list[str]:
    return list(dict.fromkeys([*first, *second]))


def effective(settings: Settings, name: str) -> dict:
    """The rules that apply to mailbox `name`.

    A value the mailbox sets wins over the user's; muted senders and notes are both lists,
    the mailbox's first.
    """
    box = settings.mailboxes.get(name)
    if box is None:
        raise MailError(f"there is no mailbox named {name!r}")
    return {
        "important": box.important if box.important is not None else settings.important,
        "delivery": box.delivery if box.delivery is not None else settings.delivery,
        "muted_senders": _union(box.muted_senders, settings.muted_senders),
        "notes": _union(box.notes, settings.notes),
        "paused": box.paused,
    }


# ---- editing one mailbox section as text ----

HEADER = re.compile(
    r"""^\s*\[\s*mailboxes\s*\.\s*(?:"(?P<q>[^"\\]*)"|'(?P<l>[^']*)'|(?P<b>[A-Za-z0-9_-]+))"""
    r"""\s*\](?:\s*#.*)?$"""
)
# a line with its own ending, so that rejoining the lines gives the text back unchanged
LINE = re.compile(r"[^\n]*\n|[^\n]+")
ESCAPES = {
    "\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r",
}


def _refused(name: str) -> MailError:
    return MailError(f'config.toml: cannot edit [mailboxes."{name}"] in place; edit it by hand')


def _eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _spacing(line: str) -> bool:
    """A blank line or a comment line: what trails a section without belonging to its keys."""
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _find(lines: list[str], name: str) -> tuple[int, int] | None:
    """Line range [start, end) of the section headed `[mailboxes."<name>"]`, or None."""
    for start, line in enumerate(lines):
        match = HEADER.match(line.rstrip("\r\n"))
        if match is None:
            continue
        if next(v for v in match.group("q", "l", "b") if v is not None) == name:
            end = next(
                (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
                len(lines),
            )
            return start, end
    return None


def _body_end(lines: list[str], start: int, end: int) -> int:
    """One past the last line of the section that is not blank or a comment."""
    last = end
    while last > start + 1 and _spacing(lines[last - 1]):
        last -= 1
    return last


def _statements(lines: list[str], first: int, last: int, name: str) -> list[tuple[int, int, dict]]:
    """The `key = value` statements in lines[first:last] as (start, end, parsed) triples.

    A value written over several lines is one statement: it ends at the first line from which
    the lines parse.
    """
    found = []
    i = first
    while i < last:
        if _spacing(lines[i]):
            i += 1
            continue
        for j in range(i + 1, last + 1):
            try:
                parsed = tomllib.loads("".join(lines[i:j]))
            except tomllib.TOMLDecodeError:
                continue
            found.append((i, j, parsed))
            i = j
            break
        else:
            raise _refused(name)
    return found


def _trailing_comment(line: str) -> str:
    """The spaces and `# comment` after the value on a one-line `key = value`, or ""."""
    body = line.rstrip("\r\n")
    whole = tomllib.loads(body)
    for at, char in enumerate(body):
        if char == "#":
            try:
                same = tomllib.loads(body[:at]) == whole
            except tomllib.TOMLDecodeError:  # the # is inside a string
                continue
            if same:
                return body[len(body[:at].rstrip()) :]
    return ""


def _quote(text: str) -> str:
    out = []
    for char in text:
        if char in ESCAPES:
            out.append(ESCAPES[char])
        elif char < " " or char == "\x7f":
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def _render(key: str, value) -> list[str]:
    """The lines, without line endings, that set `key` to `value`."""
    if isinstance(value, bool):
        return [f"{key} = {'true' if value else 'false'}"]
    if isinstance(value, str):
        return [f"{key} = {_quote(value)}"]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        if not value:
            return [f"{key} = []"]
        return [f"{key} = [", *(f"  {_quote(v)}," for v in value), "]"]
    raise MailError(
        f"{key}: {value!r} cannot be written (use text, true or false, or a list of texts)"
    )


def _check_result(before: dict, new_text: str, name: str, table: dict | None) -> None:
    """`new_text` must parse to `before` with only mailbox `name` changed to `table`.

    A `table` of None means the mailbox is gone.
    """
    expected = dict(_boxes(before))
    if table is None:
        expected.pop(name, None)
    else:
        expected[name] = table
    _check_boxes(before, new_text, name, expected)


def _check_boxes(before: dict, new_text: str, name: str, expected: dict) -> None:
    """`new_text` must parse to `before` with the mailboxes `expected` and nothing else
    changed; else the edit of mailbox `name` is refused."""
    try:
        after = tomllib.loads(new_text.removeprefix("\ufeff"))
    except tomllib.TOMLDecodeError:
        raise _refused(name) from None

    def rest(data: dict) -> dict:
        return {k: v for k, v in data.items() if k != "mailboxes"}

    if rest(after) != rest(before) or after.get("mailboxes", {}) != expected:
        raise _refused(name)


def add_mailbox_text(text: str, mb: Mailbox) -> str:
    """`text` with a `[mailboxes."<name>"]` section for `mb` at the end.

    Only settings that differ from the defaults are written: paused, important, delivery,
    muted_senders and notes. MailError when the mailbox is not valid or its name is taken.
    """
    _check_name(mb.name)
    table = {
        "address": mb.address, "provider": mb.provider, "imap": mb.imap, "smtp": mb.smtp,
        "added": mb.added,
    }
    for key, default in OPTIONAL_DEFAULTS.items():
        value = getattr(mb, key)
        # a wrong type is written too, so that the check below can name it
        if value != default or type(value) is not type(default):
            table[key] = value
    _mailbox(mb.name, table)
    before = _toml(text)
    if mb.name in _boxes(before):
        raise MailError(f'config.toml: there is already a mailbox named "{mb.name}"')

    lines = LINE.findall(text)
    eol = _eol(text)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += eol
    if lines and lines[-1].strip():
        lines.append(eol)
    lines.append(f'[mailboxes."{mb.name}"]{eol}')
    for key in MAILBOX_KEYS:
        if key in table:
            lines += [line + eol for line in _render(key, table[key])]
    new_text = "".join(lines)
    _check_result(before, new_text, mb.name, table)
    return new_text


def remove_mailbox_text(text: str, name: str) -> str:
    """`text` without the section of mailbox `name`; the blank line before it goes too."""
    _check_name(name)
    before = _toml(text)
    if name not in _boxes(before):
        raise MailError(f'config.toml: there is no mailbox named "{name}"')
    lines = LINE.findall(text)
    span = _find(lines, name)
    if span is None:
        raise _refused(name)
    start, end = span
    last = _body_end(lines, start, end)
    # blank lines that end the file go with the section; a comment there stays
    stop = len(lines) if all(not line.strip() for line in lines[last:]) else last
    first = start - 1 if start > 0 and not lines[start - 1].strip() else start
    del lines[first:stop]
    new_text = "".join(lines)
    _check_result(before, new_text, name, None)
    return new_text


def rename_mailbox_text(text: str, name: str, new_name: str) -> str:
    """`text` with the section of mailbox `name` headed `[mailboxes."<new_name>"]`.

    Only the header line changes; its indentation and a comment after it stay. MailError when
    there is no such mailbox or the new name is taken or unusable.
    """
    _check_name(name)
    _check_name(new_name)
    before = _toml(text)
    boxes = _boxes(before)
    if name not in boxes:
        raise MailError(f'config.toml: there is no mailbox named "{name}"')
    if new_name == name:
        return text
    if new_name in boxes:
        raise MailError(f'config.toml: there is already a mailbox named "{new_name}"')
    lines = LINE.findall(text)
    span = _find(lines, name)
    if span is None:
        raise _refused(name)
    start = span[0]
    line = lines[start]
    body = line.rstrip("\r\n")
    match = HEADER.match(body)
    group = next(g for g in ("q", "l", "b") if match.group(g) is not None)
    close = body.index("]", match.end(group))
    indent = body[: len(body) - len(body.lstrip())]
    lines[start] = f"{indent}[mailboxes.{_quote(new_name)}]{body[close + 1:]}{line[len(body):]}"
    new_text = "".join(lines)
    expected = {k: v for k, v in boxes.items() if k != name}
    expected[new_name] = boxes[name]
    _check_boxes(before, new_text, name, expected)
    return new_text


def set_mailbox_value_text(text: str, name: str, key: str, value) -> str:
    """`text` with `key` of mailbox `name` set to `value`; None removes an optional key.

    The key is changed where it stands, with its comment on the same line; a key that is not
    there yet goes after the last key of the section. MailError when the mailbox would not be
    valid afterwards.
    """
    _check_name(name)
    if key not in MAILBOX_KEYS:
        raise MailError(
            f"{key!r} is not a mailbox setting (the settings are {', '.join(MAILBOX_KEYS)})"
        )
    before = _toml(text)
    old = _boxes(before).get(name)
    if old is None:
        raise MailError(f'config.toml: there is no mailbox named "{name}"')
    if not isinstance(old, dict):
        raise MailError(f'config.toml: [mailboxes."{name}"] must be a table')
    table = dict(old)
    if value is None:
        table.pop(key, None)
    else:
        table[key] = value
    _mailbox(name, table)

    lines = LINE.findall(text)
    span = _find(lines, name)
    if span is None:
        raise _refused(name)
    start, end = span
    last = _body_end(lines, start, end)
    eol = _eol(text)
    statements = _statements(lines, start + 1, last, name)
    found = [(i, j) for i, j, parsed in statements if list(parsed) == [key]]
    if found:
        i, j = found[0]
        if value is None:
            del lines[i:j]
        else:
            new = _render(key, value)
            if j == i + 1:  # a comment after a one-line value stays
                new[0] += _trailing_comment(lines[i])
            indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
            ending = lines[j - 1][len(lines[j - 1].rstrip("\r\n")) :]
            lines[i:j] = [indent + line + eol for line in new[:-1]] + [indent + new[-1] + ending]
    elif value is not None:
        if not lines[last - 1].endswith("\n"):
            lines[last - 1] += eol
        lines[last:last] = [line + eol for line in _render(key, value)]
    new_text = "".join(lines)
    _check_result(before, new_text, name, table)
    return new_text

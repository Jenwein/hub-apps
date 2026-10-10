"""The ledger itself: loading it, printing entries, and writing to it behind a guard."""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from beancount import loader
from beancount.core.data import Posting, Transaction
from beancount.parser import parser, printer

from .settings import LedgerError

UNSORTED_EXPENSE = "Expenses:Unsorted"
UNSORTED_INCOME = "Income:Unsorted"
UNMAPPED = "Assets:Unmapped"
# Accounts that mark entries which still need the user. They are never opened.
PLACEHOLDERS = frozenset({UNSORTED_EXPENSE, UNSORTED_INCOME, UNMAPPED})

_FUNDING_ROOTS = ("Assets:", "Liabilities:")


def load(main: Path) -> tuple[list, list, dict]:
    """Load the ledger: (entries, errors, options)."""
    # beancount's pickle cache keys on the files it saw. It does not notice a new file that the
    # include glob now matches (a new year file), so it could show a stale ledger to the guard.
    loader.initialize(use_cache=False)
    return loader.load_file(str(main))


def parse_text(text: str) -> list:
    """The entries in beancount text. Any parse error is a LedgerError."""
    entries, errors, _ = parser.parse_string(text)
    if errors:
        found = []
        for error in errors:
            line = (error.source or {}).get("lineno")
            found.append(f"line {line}: {error.message}" if line else error.message)
        raise LedgerError("cannot parse the entries: " + "; ".join(found))
    return entries


def format_entry(entry) -> str:
    """The beancount text of one entry, ending with a newline."""
    text = printer.format_entry(entry)
    return text if text.endswith("\n") else text + "\n"


def transactions(entries) -> list[Transaction]:
    return [entry for entry in entries if isinstance(entry, Transaction)]


def funding_postings(txn: Transaction) -> list[Posting]:
    """The postings on the user's own accounts: where the money came from or went to."""
    return [p for p in txn.postings if p.account.startswith(_FUNDING_ROOTS)]


def is_transfer(txn: Transaction) -> bool:
    """True when money only moves between the user's own accounts."""
    return bool(txn.postings) and all(p.account.startswith(_FUNDING_ROOTS) for p in txn.postings)


def year_file(ledger_dir: Path, year: int) -> Path:
    return ledger_dir / "txns" / f"{year}.beancount"


def digest(ledger_dir: Path) -> str:
    """SHA-256 over the ledger: every `.beancount` file under `ledger_dir` except in `inbox/`.

    Each file adds its path relative to `ledger_dir` (POSIX) and its bytes, in path order: any
    change to an entry, a new or renamed file, makes another digest.
    """
    ledger_dir = Path(ledger_dir)
    files = {}
    for path in ledger_dir.rglob("*.beancount"):
        relative = path.relative_to(ledger_dir)
        if relative.parts[0] != "inbox" and path.is_file():
            files[relative.as_posix()] = path
    found = hashlib.sha256()
    for name in sorted(files):
        data = files[name].read_bytes()
        found.update(f"{name}\0{len(data)}\0".encode())
        found.update(data)
    return found.hexdigest()


def _error_counts(main: Path) -> Counter:
    return Counter(error.message for error in load(main)[1])


def _describe(counts: Counter) -> str:
    return "; ".join(
        message if n == 1 else f"{message} (x{n})" for message, n in sorted(counts.items()))


def _restore(ledger_dir: Path, snapshot: dict[Path, bytes]) -> None:
    for path in ledger_dir.rglob("*.beancount"):
        if path not in snapshot and path.is_file():
            path.unlink()
    for path, data in snapshot.items():
        if not path.is_file() or path.read_bytes() != data:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)


@contextmanager
def guarded(ledger_dir: Path) -> Iterator[None]:
    """Run a write to the ledger. Undo it if beancount reports an error it did not report before.

    On an error that is new, or on an exception in the block, every `.beancount` file under
    `ledger_dir` goes back to the bytes it had at the start and files created meanwhile are
    deleted. Errors the ledger already had do not count.
    """
    ledger_dir = Path(ledger_dir)
    main = ledger_dir / "main.beancount"
    if not main.is_file():
        raise LedgerError(f"no main.beancount in {ledger_dir}")
    snapshot = {p: p.read_bytes() for p in ledger_dir.rglob("*.beancount") if p.is_file()}
    before = _error_counts(main)
    try:
        yield
        new = _error_counts(main) - before
        if new:
            raise LedgerError(f"ledger check failed: {_describe(new)}")
    except BaseException:
        _restore(ledger_dir, snapshot)
        raise


def _ends_with_newline(path: Path) -> bool:
    with path.open("rb") as stream:
        stream.seek(-1, 2)
        return stream.read(1) == b"\n"


def append_entries(ledger_dir: Path, entries: list) -> list[Path]:
    """Append entries to their year files, a blank line before each. Returns the files touched.

    Opens no guard of its own: call it inside `guarded`.
    """
    by_year: dict[int, list] = defaultdict(list)
    for entry in entries:
        by_year[entry.date.year].append(entry)
    touched = []
    for year, group in sorted(by_year.items()):
        path = year_file(ledger_dir, year)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            text = f"; Transactions of {year}\n"
        else:
            text = "" if path.stat().st_size == 0 or _ends_with_newline(path) else "\n"
        text += "".join("\n" + format_entry(entry) for entry in group)
        with path.open("a", encoding="utf-8", newline="\n") as out:
            out.write(text)
        touched.append(path)
    return touched


def _via_chat(entry):
    if isinstance(entry, Transaction) and "via" not in entry.meta:
        return entry._replace(meta={**entry.meta, "via": "chat"})
    return entry


def add_text(ledger_dir: Path, text: str) -> dict:
    """Write the entries in `text` to the ledger. Transactions without `via` get `via: "chat"`."""
    ledger_dir = Path(ledger_dir)
    entries = [_via_chat(entry) for entry in parse_text(text)]
    with guarded(ledger_dir):
        files = append_entries(ledger_dir, entries)
    return {"written": len(entries), "files": [p.relative_to(ledger_dir).as_posix() for p in files]}

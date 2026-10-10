"""Apply a plan written by `lk match`: fix chat entries, append the new ones, write the balances.

Everything that can be checked is checked before the first byte is written: the plan, the decisions,
every fix against the file it points into, the document and its target. The write itself is one
`guarded` block (fixes, new entries and balances), so any new beancount error undoes all of it.
Only then the document is copied and the plan marked applied, which is what stops a second apply.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from beancount.core.amount import Amount
from beancount.core.data import Balance, Close, Open, Posting, Transaction

from .book import (
    UNMAPPED,
    UNSORTED_EXPENSE,
    UNSORTED_INCOME,
    append_entries,
    guarded,
    load,
    parse_text,
)
from .settings import LedgerError

STALE = "ledger changed since match; run match again"
DECISIONS = ("new", "skip")
ITEM_KEYS = frozenset({"id", "kind", "date", "text", "flags", "existing", "fix"})
FIX_KEYS = frozenset({"file", "line", "account_from", "account_to", "meta"})
# Flags that keep an entry out of the ledger: it still needs the user.
BLOCKING = ("unsorted", "unmapped")
_PLACEHOLDER_FLAG = {
    UNSORTED_EXPENSE: "unsorted", UNSORTED_INCOME: "unsorted", UNMAPPED: "unmapped"}
_META_KEY = re.compile(r"[a-z][A-Za-z0-9_-]*")
_META_LINE = re.compile(r"[ \t]+([a-z][A-Za-z0-9_-]*):")


class NeedsDecision(Exception):
    """The plan cannot be applied yet. `payload` says what is missing, for the command line."""

    def __init__(self, payload: dict):
        super().__init__(json.dumps(payload, ensure_ascii=False))
        self.payload = payload


def _read_plan(path: Path) -> dict:
    try:
        plan = json.loads(path.read_text("utf-8-sig"))
    except OSError as err:
        raise LedgerError(f"cannot read the plan {path}: {err}") from err
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise LedgerError(f"{path} is not a plan written by lk match: {err}") from err
    items = plan.get("items") if isinstance(plan, dict) else None
    if (
        not isinstance(items, list) or plan.get("version") != 1
        or not all(isinstance(item, dict) and ITEM_KEYS <= item.keys() for item in items)
    ):
        raise LedgerError(f"{path} is not a plan written by lk match")
    return plan


def _check_decisions(items: list[dict], decisions: dict[str, str]) -> None:
    kinds = {item["id"]: item["kind"] for item in items}
    for id_, choice in decisions.items():
        if id_ not in kinds:
            raise LedgerError(f"no item {id_} in the plan")
        if kinds[id_] != "uncertain":
            raise LedgerError(f"{id_} is {kinds[id_]}, not uncertain: it takes no decision")
        if choice not in DECISIONS:
            raise LedgerError(f"the decision for {id_} must be new or skip, got {choice!r}")


def _to_write(items: list[dict], decisions: dict[str, str]) -> list[dict]:
    """The items that become ledger entries: new ones, and uncertain ones decided `new`."""
    return [
        item for item in items
        if item["kind"] == "new" or (item["kind"] == "uncertain" and decisions[item["id"]] == "new")
    ]


def _parse_item(item: dict) -> list:
    try:
        return parse_text(item["text"])
    except LedgerError as err:
        raise LedgerError(f"item {item['id']}: {err}") from err


def _blocking(item: dict, entries: list) -> list[str]:
    """The flags of an item to write that keep it out: its own, and any placeholder it holds."""
    flags = list(item["flags"])
    for entry in entries:
        for posting in getattr(entry, "postings", None) or []:
            flag = _PLACEHOLDER_FLAG.get(posting.account)
            if flag and flag not in flags:
                flags.append(flag)
    return flags if any(flag in BLOCKING for flag in flags) else []


# --- fixes -----------------------------------------------------------------------------------


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _is_indented(line: str) -> bool:
    return line[:1] in (" ", "\t") and bool(line.strip())


def _existing_header(item: dict) -> Transaction:
    """The entry the fix is for, as the plan recorded it at match time."""
    try:
        [txn] = parse_text(item["existing"][0]["text"])
        assert isinstance(txn, Transaction)
    except (LedgerError, IndexError, KeyError, TypeError, ValueError, AssertionError) as err:
        raise LedgerError(f"item {item['id']} has no usable existing entry") from err
    return txn


def _heading(txn: Transaction) -> tuple[date, str, str]:
    """What names an entry: its date, payee and narration. The printer drops an empty payee."""
    return txn.date, txn.payee or "", txn.narration or ""


def _stated(posting: Posting) -> tuple[Decimal, str | None] | None:
    """The amount a posting states: (number, currency), the currency None when it is elided."""
    units = posting.units
    if isinstance(units, Amount) and isinstance(units.number, Decimal):
        return units.number, units.currency if isinstance(units.currency, str) else None
    return None


def _same_postings(txn: Transaction, expected: Transaction) -> bool:
    """The same accounts in the same order, and the same amounts wherever the file states one.

    The plan holds the entry as beancount loaded it, with elided amounts filled in; an amount the
    file leaves out for beancount to work out is not a difference.
    """
    if [p.account for p in txn.postings] != [p.account for p in expected.postings]:
        return False
    for ours, theirs in zip(txn.postings, expected.postings, strict=True):
        found = _stated(ours)
        if found is None:
            continue
        planned = _stated(theirs)
        if planned is None or found[0] != planned[0]:
            return False
        if found[1] is not None and planned[1] is not None and found[1] != planned[1]:
            return False
    return True


def _check_entry(lines: list[str], start: int, end: int, expected: Transaction) -> None:
    """The lines [start, end) must be the entry the plan saw.

    Same date, payee, narration and postings: a line number alone can point at another entry of
    the same day once the file has changed (a hand edit, or another plan applied first).
    """
    if not lines[start].startswith(expected.date.isoformat()):
        raise LedgerError(STALE)
    try:
        [txn] = parse_text("\n".join(line.rstrip("\r") for line in lines[start:end]) + "\n")
    except (LedgerError, ValueError):
        raise LedgerError(STALE) from None
    if (
        not isinstance(txn, Transaction) or _heading(txn) != _heading(expected)
        or not _same_postings(txn, expected)
    ):
        raise LedgerError(STALE)


def _fix_entry(lines: list[str], item: dict) -> None:
    """Edit the entry of one fix in `lines`: its account and the metadata under its header."""
    fix = item["fix"]
    index = fix["line"] - 1
    if not 0 <= index < len(lines):
        raise LedgerError(STALE)
    end = index + 1
    while end < len(lines) and _is_indented(lines[end]):
        end += 1
    _check_entry(lines, index, end, _existing_header(item))

    old, new = fix["account_from"], fix["account_to"]
    if new != old:
        # A posting line: indentation, an optional flag, then the account. The amount may be absent.
        posting = re.compile(r"([ \t]+(?:\S[ \t]+)?)" + re.escape(old) + r"(?=[ \t;\r]|$)")
        for n in range(index + 1, end):
            found = posting.match(lines[n])
            if found:
                lines[n] = found.group(1) + new + lines[n][found.end():]
                break
        else:
            raise LedgerError(STALE)

    meta = fix["meta"]
    for key, value in meta.items():
        if not _META_KEY.fullmatch(key) or not isinstance(value, str):
            raise LedgerError(f"item {item['id']}: bad metadata {key!r}")
    present = set()
    for n in range(index + 1, end):  # the entry's own metadata comes before its postings
        found = _META_LINE.match(lines[n])
        if found:
            present.add(found.group(1))
        elif not lines[n].lstrip().startswith(";"):
            break
    indent = re.match(r"[ \t]*", lines[index + 1]).group() if end > index + 1 else "  "
    eol = "\r" if lines[index].endswith("\r") else ""
    lines[index + 1:index + 1] = [
        f"{indent}{key}: {_quote(value)}{eol}" for key, value in meta.items() if key not in present]


def _fix_files(ledger_dir: Path, fixes: list[dict]) -> dict[Path, bytes]:
    """The new content of every file the fixes touch. Nothing is written here.

    The fixes of one file go from its last entry up, so that the line numbers of the ones still to
    do are not moved by the lines inserted below them.
    """
    root = ledger_dir.resolve()
    by_file = defaultdict(list)
    seen = set()
    for item in fixes:
        fix = item["fix"]
        if not isinstance(fix, dict) or not FIX_KEYS <= fix.keys() or not isinstance(
                fix["line"], int) or not isinstance(fix["meta"], dict):
            raise LedgerError(f"item {item['id']} has a malformed fix")
        path = Path(fix["file"]).resolve() if fix["file"] else None
        if path is None or path.suffix != ".beancount" or not path.is_relative_to(root):
            raise LedgerError(f"item {item['id']}: {fix['file']!r} is not in the ledger {root}")
        if (path, fix["line"]) in seen:
            raise LedgerError(f"item {item['id']}: another fix is for the same entry")
        seen.add((path, fix["line"]))
        by_file[path].append(item)
    changed = {}
    for path, group in by_file.items():
        try:
            lines = path.read_bytes().decode("utf-8").split("\n")
        except FileNotFoundError:
            raise LedgerError(STALE) from None
        except (OSError, UnicodeDecodeError) as err:
            raise LedgerError(f"cannot read {path}: {err}") from err
        for item in sorted(group, key=lambda found: found["fix"]["line"], reverse=True):
            _fix_entry(lines, item)
        changed[path] = "\n".join(lines).encode("utf-8")
    return changed


# --- balances and documents ------------------------------------------------------------------


def _new_balances(texts: object, entries: list) -> list:
    """The plan's balance directives that the ledger does not hold yet.

    A bill imported again brings the same balance line again; it must not be written twice.
    """
    if not isinstance(texts, list) or not all(isinstance(text, str) for text in texts):
        raise LedgerError("the plan's balances must be a list of text lines")
    parsed = parse_text("\n".join(texts) + "\n") if texts else []
    if not all(isinstance(entry, Balance) for entry in parsed):
        raise LedgerError("the plan's balances must be balance directives")

    def key(entry: Balance) -> tuple:
        return entry.date, entry.account, entry.amount.number, entry.amount.currency

    known = {key(entry) for entry in entries if isinstance(entry, Balance)}
    fresh = []
    for entry in parsed:
        if key(entry) not in known:
            known.add(key(entry))
            fresh.append(entry)
    return fresh


def _document_target(
        ledger_dir: Path, entries: list, document: Path | None, account: str | None, when: str,
) -> Path | None:
    """Where the document goes, after checking that it can go there. None without a document."""
    if (document is None) != (account is None):
        raise LedgerError("--document and --document-account go together")
    if document is None:
        return None
    if not document.is_file():
        raise LedgerError(f"no such file: {document}")
    try:
        day = date.fromisoformat(when)
    except ValueError:
        raise LedgerError(f"the plan has an item dated {when!r}") from None
    opened = [e for e in entries if isinstance(e, Open) and e.account == account]
    if not opened:
        raise LedgerError(f"the document account {account} is not open in the ledger")
    if opened[0].date > day or any(
            isinstance(e, Close) and e.account == account and e.date < day for e in entries):
        raise LedgerError(f"the document account {account} is not open on {when}")
    target = ledger_dir / "documents" / Path(*account.split(":")) / f"{when}.{document.name}"
    if target.exists():
        raise LedgerError(f"{target} already exists; it is not overwritten")
    return target


def _copy_document(source: Path, target: Path) -> None:
    """Copy the file byte for byte; never over a file that is there."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with source.open("rb") as src, target.open("xb") as dst:
            shutil.copyfileobj(src, dst)
    except BaseException:
        if target.exists():
            target.unlink()
        raise
    try:  # the bill keeps its date, but that is no reason to fail
        shutil.copystat(source, target)
    except OSError:
        pass


def _save_plan(path: Path, plan: dict) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", "utf-8", newline="\n")
    os.replace(temp, path)


# --- apply -----------------------------------------------------------------------------------


def apply_plan(
        plan_path: Path, ledger_dir: Path, decisions: dict[str, str],
        document: Path | None = None, document_account: str | None = None,
) -> dict:
    """Apply a plan to the ledger. Returns {"written", "skipped", "fixed", "balances", "document"}.

    `decisions` maps the id of each uncertain item to "new" (write it) or "skip". Raises
    `NeedsDecision` when an uncertain item has no decision yet, or when an item to write still has
    an unsorted or unmapped account; nothing is written then.
    """
    plan_path, ledger_dir = Path(plan_path), Path(ledger_dir)
    plan = _read_plan(plan_path)
    if plan.get("applied"):
        raise LedgerError("plan already applied")
    items = plan["items"]
    _check_decisions(items, decisions)
    undecided = [i["id"] for i in items if i["kind"] == "uncertain" and i["id"] not in decisions]
    if undecided:
        raise NeedsDecision({"undecided": undecided})

    writing = _to_write(items, decisions)
    new_entries, unresolved = [], []
    for item in writing:
        entries = _parse_item(item)
        flags = _blocking(item, entries)
        if flags:
            unresolved.append({"id": item["id"], "flags": flags})
        new_entries.extend(entries)
    if unresolved:
        raise NeedsDecision({"unresolved": unresolved})

    main = ledger_dir / "main.beancount"
    if not main.is_file():
        raise LedgerError(f"no main.beancount in {ledger_dir}")
    existing = load(main)[0]
    fixes = [item for item in items if item["kind"] == "fix"]
    changed = _fix_files(ledger_dir, fixes)
    balances = _new_balances(plan.get("balances", []), existing)
    when = max((item["date"] for item in items), default=date.today().isoformat())
    target = _document_target(ledger_dir, existing, document, document_account, when)

    with guarded(ledger_dir):
        for path, data in changed.items():
            path.write_bytes(data)
        append_entries(ledger_dir, new_entries + balances)

    skipped = [
        item for item in items
        if item["kind"] == "duplicate" or (
            item["kind"] == "uncertain" and decisions[item["id"]] == "skip")]
    result = {
        "written": len(new_entries),
        "skipped": len(skipped),
        "fixed": len(fixes),
        "balances": len(balances),
        "document": None,
    }
    problem = None
    if target is not None:
        try:
            _copy_document(document, target)
            result["document"] = target.relative_to(ledger_dir).as_posix()
        except OSError as err:
            problem = f"the document could not be copied: {err}"
    plan["applied"], plan["result"] = True, result
    try:
        _save_plan(plan_path, plan)
    except OSError as err:
        raise LedgerError(
            f"the entries are in the ledger, but the plan could not be marked applied ({err}); "
            "do not apply it again") from err
    if problem:
        raise LedgerError(
            f"the entries are in the ledger and the plan is marked applied, but {problem}")
    return result

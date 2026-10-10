"""Turn a bill into beancount entries with double-entry-generator.

The bill is passed to the converter as it is. What comes back is cleaned up: every posting the
converter could not place goes to a placeholder account (see `book.PLACEHOLDERS`), every entry gets
`via`, and a bank statement's closing balance becomes a `balance` directive when it is clear.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import yaml
from beancount.core.amount import Amount
from beancount.core.data import Balance, Posting, Transaction, new_metadata

from .book import (
    PLACEHOLDERS,
    UNMAPPED,
    UNSORTED_EXPENSE,
    UNSORTED_INCOME,
    format_entry,
    parse_text,
    transactions,
)
from .settings import LedgerError, Settings, Source

# A peer no counterparty is called. Rules that name it never match.
NEVER = "__ledger_never_matches__"

_FUNDING_ROOTS = ("Assets:", "Liabilities:")
# What double-entry-generator writes for a balance: Go's %G, so large values use an exponent.
_PLAIN_DECIMAL = re.compile(r"-?\d+(\.\d+)?")
_MESSAGE_LINES = 6


def _rules(path: Path) -> list[dict]:
    """The `rules` list of a rules file; a missing or empty file has none."""
    if not path.is_file():
        return []
    try:
        loaded = yaml.safe_load(path.read_text("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as err:
        raise LedgerError(f"cannot read {path}: {err}") from err
    if loaded is None:
        return []
    if not isinstance(loaded, dict):
        raise LedgerError(f"{path}: expected a `rules:` list of rules")
    rules = loaded.get("rules") or []
    if not isinstance(rules, list) or not all(isinstance(rule, dict) for rule in rules):
        raise LedgerError(f"{path}: expected a `rules:` list of rules")
    return rules


def build_config(
    src: Source,
    settings: Settings,
    defaults_dir: Path,
    ledger_dir: Path,
    account: str | None,
) -> dict:
    """The double-entry-generator config for one source.

    double-entry-generator lets the last matching rule win, field by field. So the app's default
    rules come first and the user's rules (`<ledger_dir>/importers/<source>.yaml`) after them.
    """
    if src.provider is None:
        raise LedgerError(f"{src.name} has no converter; read the file with lk table")
    rules = _rules(Path(defaults_dir) / f"{src.name}.yaml")
    rules += _rules(Path(ledger_dir) / "importers" / f"{src.name}.yaml")
    config = {
        "title": "ledger",
        "defaultCurrency": settings.currency,
        "defaultMinusAccount": UNMAPPED,
        "defaultPlusAccount": UNSORTED_EXPENSE,
        # WeChat stops with an error on a row with a fee unless this is set.
        "defaultCommissionAccount": UNSORTED_EXPENSE,
    }
    if src.kind == "bank":
        if not account:
            raise LedgerError("--account is required for bank statements")
        config["defaultCashAccount"] = account
        # A bank's card account only applies when its rule list is not empty.
        rules.append({"peer": NEVER, "targetAccount": UNSORTED_EXPENSE})
    config[src.provider] = {"rules": rules}
    return config


def _is_default(account: str) -> bool:
    """True for an account the converter fell back to: a FIXME account or one of the defaults."""
    return "FIXME" in account.split(":") or account in PLACEHOLDERS


def _placeholder(posting: Posting) -> Posting:
    if not _is_default(posting.account):
        return posting
    if posting.account.startswith(_FUNDING_ROOTS):
        return posting._replace(account=UNMAPPED)
    number = posting.units.number if posting.units else None
    income = number is not None and number < 0
    return posting._replace(account=UNSORTED_INCOME if income else UNSORTED_EXPENSE)


def normalize(entries: list, via: str) -> list:
    """The entries with `via` on every transaction and placeholders for unplaced postings."""
    result = []
    for entry in entries:
        if isinstance(entry, Transaction):
            entry = entry._replace(
                meta={**(entry.meta or {}), "via": via},
                postings=[_placeholder(p) for p in entry.postings],
            )
        result.append(entry)
    return result


def _plain_decimal(value) -> Decimal | None:
    text = str(value).strip()
    return Decimal(text) if _PLAIN_DECIMAL.fullmatch(text) else None


def closing_balance(entries: list, account: str, currency: str) -> Balance | None:
    """A `balance` directive for the day after the last row of a bank statement, if it is clear.

    Bank providers put the running balance of every row in the `balances` metadata. It is the
    closing balance only when exactly one row falls on the latest date (the order of rows within a
    day is not known) and the value is a plain decimal (a value with an exponent was rounded).
    """
    if not account.startswith("Assets:"):
        return None
    rows = [(t.date, t.meta["balances"]) for t in transactions(entries) if "balances" in t.meta]
    if not rows:
        return None
    latest = max(day for day, _ in rows)
    last = [value for day, value in rows if day == latest]
    if len(last) != 1:
        return None
    number = _plain_decimal(last[0])
    if number is None:
        return None
    return Balance(
        new_metadata("<convert>", 0), latest + timedelta(days=1), account,
        Amount(number, currency), None, None)


def _message(text: str) -> str:
    """The end of what the converter printed: its log lines and the error that stopped it."""
    lines = [line for line in text.splitlines() if line.strip()]
    for number, line in enumerate(lines):
        if line.startswith("goroutine "):  # a Go panic: the stack trace after it is noise
            lines = lines[:number]
            break
    return "\n".join(lines[-_MESSAGE_LINES:])


def _run(deb: str, args: list[str]) -> None:
    exe = shutil.which(deb)
    if exe is None:
        raise LedgerError(f"{deb} is not installed or not on PATH; see SETUP.md")
    # A list, never a shell: bill paths may hold spaces and non-ASCII characters.
    result = subprocess.run(
        [exe, *args], check=False, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    if result.returncode != 0:
        detail = _message(result.stderr) or _message(result.stdout) or "no message"
        raise LedgerError(f"{deb} failed (exit {result.returncode}): {detail}")


def convert(
    src: Source,
    bill: Path,
    out: Path,
    *,
    settings: Settings,
    defaults_dir: Path,
    ledger_dir: Path,
    account: str | None,
    deb: str = "double-entry-generator",
) -> dict:
    """Convert `bill` and write the entries to `out`. Returns what was staged.

    `out` is written only when the conversion succeeded. The result is
    `{"staged": <out>, "entries": <transactions>, "balance": <balance directive text> | None}`.
    """
    bill, out = Path(bill), Path(out)
    config = build_config(src, settings, defaults_dir, ledger_dir, account)
    if not bill.is_file():
        raise LedgerError(f"no such file: {bill}")
    with tempfile.TemporaryDirectory(prefix="lk-convert-") as tmp:
        config_path = Path(tmp) / "config.yaml"
        raw_path = Path(tmp) / "converted.beancount"
        config_path.write_text(
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False), "utf-8", newline="\n")
        _run(deb, [
            "translate", "-p", src.provider, "-t", "beancount", "--config", str(config_path),
            "-o", str(raw_path), *src.extra_args, str(bill),
        ])
        if not raw_path.is_file():
            raise LedgerError(f"{deb} wrote no output for {bill}")
        converted = parse_text(raw_path.read_text("utf-8-sig"))
    entries = normalize(transactions(converted), src.name)
    if not entries:
        raise LedgerError(f"no transactions in {bill}; is it a {src.name} export?")
    balance = closing_balance(entries, account, settings.currency) if account else None
    staged = [*entries, balance] if balance else entries
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(format_entry(entry) for entry in staged), "utf-8", newline="\n")
    return {
        "staged": str(out),
        "entries": len(entries),
        "balance": format_entry(balance).strip() if balance else None,
    }

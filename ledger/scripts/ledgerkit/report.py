"""Numbers out of the ledger: period totals, net worth, open balances and the monthly summary.

All sums are Decimal. `summary` turns them into strings with two decimals, so that no float
reaches the JSON. Nothing here imports matplotlib; `chart` imports this module.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from beancount.core import prices

from . import book
from .book import funding_postings, transactions
from .settings import LedgerError, load_settings

RECEIVABLES = ("Assets:Receivables:",)
PAYABLES = ("Liabilities:Payables:",)

_CENT = Decimal("0.01")
_MONTH = re.compile(r"([0-9]{4})-([0-9]{2})")


def money(value: Decimal) -> str:
    """An amount as text with two decimals and its sign: `-200.00`. Never `-0.00`."""
    cents = value.quantize(_CENT)
    return str(abs(cents) if cents == 0 else cents)


def shift_month(day: date, months: int) -> date:
    """The first day of the month `months` away from the month of `day`.

    Raises ValueError when that year does not exist.
    """
    year, index = divmod(day.year * 12 + day.month - 1 + months, 12)
    return date(year, index + 1, 1)


def month_bounds(month: str) -> tuple[date, date, date]:
    """(first day of the month before, first day, first day of the month after) of `YYYY-MM`."""
    found = _MONTH.fullmatch(month)
    if found:
        try:
            first = date(int(found[1]), int(found[2]), 1)
            return shift_month(first, -1), first, shift_month(first, 1)
        except ValueError:
            pass  # month 00 or 13, or a neighbour month outside the years date supports
    raise LedgerError(f"the month must be YYYY-MM, got {month!r}")


def load_ledger(main: Path) -> tuple[list, str]:
    """(entries, currency) of the ledger whose main file is `main`.

    The currency is the one in config.toml, next to main.beancount. Errors that beancount reports
    for the ledger do not stop a report: `lk` has other ways to show them.
    """
    main = Path(main)
    if not main.is_file():
        raise LedgerError(f"no main.beancount in {main.parent}")
    currency = load_settings(main.parent).currency
    return book.load(main)[0], currency


def period_totals(
    entries, root: str, start: date, end: date, currency: str
) -> dict[str, Decimal]:
    """What the postings in `currency` under `root` add up to, from `start` up to `end`.

    `root` is "Expenses" or "Income". Totals are grouped by the first two components of the
    account (`Expenses:Food:Lunch` counts under `Expenses:Food`), the largest first. Income is
    negated, so that what was earned is positive.
    """
    sign = -1 if root == "Income" else 1
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for txn in transactions(entries):
        if not start <= txn.date < end:
            continue
        for posting in txn.postings:
            parts = posting.account.split(":")
            if parts[0] == root and posting.units.currency == currency:
                totals[":".join(parts[:2])] += sign * posting.units.number
    return dict(sorted(totals.items(), key=lambda item: (-item[1], item[0])))


def net_worth(entries, on: date, currency: str) -> tuple[Decimal, list[str]]:
    """What the Assets and Liabilities accounts held before `on`, in `currency`.

    Holdings in another currency (a fund, dollars) count at the latest price on or before `on`.
    A currency that has no price is left out of the sum and listed in the second value.
    """
    held: dict[str, Decimal] = defaultdict(Decimal)
    for txn in transactions(entries):
        if txn.date < on:
            for posting in funding_postings(txn):
                held[posting.units.currency] += posting.units.number
    price_map = prices.build_price_map(entries)
    total = Decimal(0)
    unpriced = []
    for held_currency, number in sorted(held.items()):
        if number == 0:
            continue
        if held_currency == currency:
            total += number
            continue
        _, rate = prices.get_price(price_map, (held_currency, currency), on)
        if rate is None:
            unpriced.append(held_currency)
        else:
            total += number * rate
    return total, unpriced


def open_balances(
    entries, prefixes: tuple[str, ...], on: date, *, currency: str | None = None
) -> dict[str, Decimal]:
    """The balances that are not zero, before `on`, of the accounts that start with a prefix.

    With a `currency` only that currency is counted. Without one, an account that holds several
    currencies is a LedgerError: its balance would be a sum of unlike things.
    """
    held: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for txn in transactions(entries):
        if txn.date >= on:
            continue
        for posting in txn.postings:
            units = posting.units
            if posting.account.startswith(prefixes) and currency in (None, units.currency):
                held[posting.account][units.currency] += units.number
    balances = {}
    for account, by_currency in sorted(held.items()):
        nonzero = {name: number for name, number in by_currency.items() if number != 0}
        if len(nonzero) > 1:
            raise LedgerError(
                f"{account} holds several currencies ({', '.join(sorted(nonzero))}); "
                "name the one to count")
        if nonzero:
            balances[account] = next(iter(nonzero.values()))
    return balances


def _texts(amounts: dict[str, Decimal]) -> dict[str, str]:
    return {account: money(number) for account, number in amounts.items()}


def _sum(amounts: dict[str, Decimal]) -> Decimal:
    return sum(amounts.values(), Decimal(0))


def _unsorted(entries) -> int:
    """How many transactions have a posting on a placeholder account: they still need the user."""
    return sum(
        1 for txn in transactions(entries)
        if any(posting.account in book.PLACEHOLDERS for posting in txn.postings))


def summary(main: Path, month: str | None, today: date) -> dict:
    """The summary of a month (`YYYY-MM`), or only what is owed when `month` is None.

    The currency is the ledger's. Receivables (`Assets:Receivables:`) and payables
    (`Liabilities:Payables:`) are the balances at the end of `today` in both forms. The net worth
    runs from before the first day of the month to before the first day of the next, and
    `unpriced` lists the currencies that are left out at either end. `unsorted` counts all the
    transactions that touch a placeholder account, whatever their date. Amounts are strings with
    two decimals; payables are negative.
    """
    bounds = month_bounds(month) if month is not None else None
    entries, currency = load_ledger(main)
    until = today + timedelta(days=1)
    receivables = _texts(open_balances(entries, RECEIVABLES, until, currency=currency))
    payables = _texts(open_balances(entries, PAYABLES, until, currency=currency))
    if bounds is None:
        return {"currency": currency, "receivables": receivables, "payables": payables}
    before, start, end = bounds
    expenses = period_totals(entries, "Expenses", start, end, currency)
    income = period_totals(entries, "Income", start, end, currency)
    start_worth, start_unpriced = net_worth(entries, start, currency)
    end_worth, end_unpriced = net_worth(entries, end, currency)

    def total(root: str, first: date, last: date) -> str:
        return money(_sum(period_totals(entries, root, first, last, currency)))

    return {
        "currency": currency,
        "month": month,
        "expenses": _texts(expenses),
        "expenses_total": money(_sum(expenses)),
        "income": _texts(income),
        "income_total": money(_sum(income)),
        "previous": {
            "expenses_total": total("Expenses", before, start),
            "income_total": total("Income", before, start),
        },
        "net_worth": {
            "start": money(start_worth),
            "end": money(end_worth),
            "unpriced": sorted(set(start_unpriced) | set(end_unpriced)),
        },
        "receivables": receivables,
        "payables": payables,
        "unsorted": _unsorted(entries),
    }

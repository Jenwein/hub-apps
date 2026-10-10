"""Match staged entries against the ledger: each one is new, a duplicate, a fix or uncertain.

Staged transactions are taken in date order and get the ids s1, s2, ... An existing entry serves at
most one staged entry. For each staged entry S the first rule that applies wins:

1. order-id: an existing entry from the same bill with the same `orderId`, or an earlier staged
   entry with both, makes S a duplicate. This rule runs over the whole batch first, so no other rule
   takes an entry that an order id claims.
2. chat: a chat entry (`via: "chat"`, no `orderId`) in the window with S's funding amount on the
   same account, or on the channel S came through. The nearest one is fixed to S's account and gets
   S's order id; several at the same distance are uncertain.
3. posting: entries in the window sharing a posting (account and amount, placeholders aside) with S.
   A bank line that names a payment channel, or matches a transfer, is a duplicate of the nearest
   one; anything else with such a candidate is uncertain.
4. A bank line that names a payment channel and matched nothing is uncertain.
5. A chat entry in the window with the same amount on another account makes S uncertain.
6. Otherwise S is new.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from beancount.core.amount import Amount
from beancount.core.data import Balance, Posting, Transaction
from beancount.core.flags import FLAG_PADDING

from .book import (
    PLACEHOLDERS,
    UNMAPPED,
    UNSORTED_EXPENSE,
    UNSORTED_INCOME,
    format_entry,
    funding_postings,
    is_transfer,
    load,
    parse_text,
    transactions,
)
from .settings import LedgerError, Settings, source

CHAT = "chat"
KINDS = ("new", "duplicate", "fix", "uncertain")
FLAGS = ("unsorted", "unmapped", "investment")
# The staged keys a fix copies onto the chat entry; `via` goes there as `bill`.
FIX_KEYS = ("orderId", "payTime")


@dataclass
class Item:
    id: str
    kind: str  # one of KINDS
    reason: str
    date: str
    text: str
    flags: list[str]
    existing: list[dict]  # {"file", "line", "text"} of the entries the decision rests on
    fix: dict | None  # {"file", "line", "account_from", "account_to", "meta"} for a fix


# A decision for one staged entry: (kind, reason, the existing entries it rests on, fix).
Decision = tuple[str, str, list[Transaction], dict | None]


def _units(posting: Posting) -> tuple[Decimal, str] | None:
    """(number, currency) of a posting whose amount is known; numbers compare by value."""
    units = posting.units
    if (isinstance(units, Amount) and isinstance(units.number, Decimal)
            and isinstance(units.currency, str)):
        return units.number, units.currency
    return None


def _days(a: Transaction, b: Transaction) -> int:
    return abs((a.date - b.date).days)


def _mentions(txn: Transaction, words: tuple[str, ...]) -> bool:
    text = f"{txn.payee or ''}\n{txn.narration or ''}"
    return any(word and word in text for word in words)


def _bill(entry: Transaction):
    """The bill an existing entry's `orderId` came from: `bill` on a fixed chat entry, or `via`."""
    return entry.meta.get("bill") or entry.meta.get("via")


def _order_key(origin, entry: Transaction) -> tuple | None:
    order = entry.meta.get("orderId")
    return None if order is None else (origin, str(order))


def _is_open_chat(entry: Transaction) -> bool:
    """A chat entry that no bill has claimed yet."""
    return entry.meta.get("via") == CHAT and "orderId" not in entry.meta


def _other_order(entry: Transaction, staged: Transaction) -> bool:
    """Both carry an order id from the same bill and the ids differ: two different payments."""
    ours, theirs = entry.meta.get("orderId"), staged.meta.get("orderId")
    return (
        _bill(entry) == staged.meta["via"] and ours is not None and theirs is not None
        and str(ours) != str(theirs)
    )


def _shares_posting(entry: Transaction, staged: Transaction) -> bool:
    keys = {
        (p.account, _units(p)) for p in staged.postings
        if p.account not in PLACEHOLDERS and _units(p) is not None
    }
    return any((p.account, _units(p)) in keys for p in entry.postings)


def _chat_pair(entry: Transaction, staged: Transaction) -> tuple[Posting, Posting] | None:
    """The funding postings that make a chat entry and a staged entry the same payment, if any."""
    pairs = [
        (ours, theirs)
        for ours in funding_postings(entry) for theirs in funding_postings(staged)
        if _units(ours) is not None and _units(ours) == _units(theirs)
    ]
    for ours, theirs in pairs:
        if ours.account == theirs.account and ours.account not in PLACEHOLDERS:
            return ours, theirs
    if pairs and entry.meta.get("channel") == staged.meta["via"]:
        return pairs[0]
    return None


def _same_size(entry: Transaction, staged: Transaction) -> bool:
    """A funding amount of the same size on another account."""
    for ours in funding_postings(entry):
        for theirs in funding_postings(staged):
            a, b = _units(ours), _units(theirs)
            if (a and b and ours.account != theirs.account
                    and a[1] == b[1] and abs(a[0]) == abs(b[0])):
                return True
    return False


def _location(entry: Transaction) -> tuple[str, int]:
    """The file and header line of an entry, from beancount's metadata."""
    filename = entry.meta.get("filename") or ""
    return (str(Path(filename)) if filename else ""), int(entry.meta.get("lineno") or 0)


def _nearest(found: list[int], ledger: list[Transaction], staged: Transaction) -> list[int]:
    """The candidates at the smallest date distance, in file order."""
    if not found:
        return []
    best = min(_days(ledger[i], staged) for i in found)
    return sorted((i for i in found if _days(ledger[i], staged) == best),
                  key=lambda i: _location(ledger[i]))


def _fix(entry: Transaction, ours: Posting, theirs: Posting, staged: Transaction) -> dict:
    file, line = _location(entry)
    meta = {key: str(staged.meta[key]) for key in FIX_KEYS if key in staged.meta}
    meta["bill"] = staged.meta["via"]
    # The bill may not know the account (a placeholder); the chat entry's account then stays.
    target = ours.account if theirs.account in PLACEHOLDERS else theirs.account
    return {"file": file, "line": line, "account_from": ours.account, "account_to": target,
            "meta": meta}


def _decide(staged: Transaction, bank: bool, ledger: list[Transaction], used: set[int],
            settings: Settings) -> Decision:
    """Rules 2 to 6 for one staged entry. Marks the entry it matches as used."""
    near = [i for i, entry in enumerate(ledger)
            if i not in used and _days(entry, staged) <= settings.window_days]

    pairs = {}
    for i in near:
        if _is_open_chat(ledger[i]):
            pair = _chat_pair(ledger[i], staged)
            if pair:
                pairs[i] = pair
    chats = _nearest(list(pairs), ledger, staged)
    if len(chats) == 1:
        [i] = chats
        used.add(i)
        return "fix", "chat", [ledger[i]], _fix(ledger[i], *pairs[i], staged)
    if chats:
        return "uncertain", "chat", [ledger[i] for i in chats], None

    channel_line = bank and _mentions(staged, settings.channel_keywords)
    candidates = _nearest(
        [i for i in near
         if not _other_order(ledger[i], staged) and _shares_posting(ledger[i], staged)],
        ledger, staged)
    if candidates:
        first = candidates[0]
        if bank and (channel_line or is_transfer(ledger[first])):
            used.add(first)
            return "duplicate", "channel" if channel_line else "transfer", [ledger[first]], None
        return "uncertain", "possible-duplicate", [ledger[i] for i in candidates], None
    if channel_line:
        return "uncertain", "channel-unmatched", [], None

    others = _nearest(
        [i for i in near if _is_open_chat(ledger[i]) and _same_size(ledger[i], staged)],
        ledger, staged)
    if others:
        return "uncertain", "possible-chat", [ledger[i] for i in others], None
    return "new", "new", [], None


def _flags(txn: Transaction, settings: Settings) -> list[str]:
    accounts = {posting.account for posting in txn.postings}
    found = []
    if accounts & {UNSORTED_EXPENSE, UNSORTED_INCOME}:
        found.append("unsorted")
    if UNMAPPED in accounts:
        found.append("unmapped")
    if _mentions(txn, settings.invest_keywords):
        found.append("investment")
    return found


def _ref(entry: Transaction) -> dict:
    file, line = _location(entry)
    return {"file": file, "line": line, "text": format_entry(entry)}


def match(existing: list, staged: list, settings: Settings) -> list[Item]:
    """Decide every staged transaction against the existing entries. See the module doc."""
    ledger = [t for t in transactions(existing) if t.flag != FLAG_PADDING]
    batch = sorted(transactions(staged), key=lambda t: t.date)
    for txn in batch:
        if "via" not in txn.meta:
            raise LedgerError(f"staged entry on {txn.date} has no via")
    banks = [source(settings, txn.meta["via"]).kind == "bank" for txn in batch]

    used: set[int] = set()
    decided: dict[int, Decision] = {}
    by_order = defaultdict(list)
    for i, entry in enumerate(ledger):
        key = _order_key(_bill(entry), entry)
        if key is not None:
            by_order[key].append(i)
    seen = set()
    for n, txn in enumerate(batch):
        key = _order_key(txn.meta["via"], txn)
        if key is None:
            continue
        found = next((i for i in by_order[key] if i not in used), None)
        if found is not None:
            used.add(found)
            decided[n] = "duplicate", "order-id", [ledger[found]], None
        elif key in seen:  # twice in this batch; the first one is decided on its own
            decided[n] = "duplicate", "order-id", [], None
        seen.add(key)

    items = []
    for n, txn in enumerate(batch):
        if n not in decided:
            decided[n] = _decide(txn, banks[n], ledger, used, settings)
        kind, reason, found, fix = decided[n]
        items.append(Item(
            id=f"s{n + 1}", kind=kind, reason=reason, date=txn.date.isoformat(),
            text=format_entry(txn), flags=_flags(txn, settings),
            existing=[_ref(entry) for entry in found], fix=fix))
    return items


def run(main: Path, staged_path: Path, out: Path, settings: Settings) -> dict:
    """Match the staged file against the ledger and write the plan to `out`. Returns a summary.

    The flag counts cover the new and uncertain items, the ones that may be written: a duplicate
    is dropped and a fix keeps the existing entry's postings apart from the funding account.
    """
    main, staged_path, out = Path(main), Path(staged_path), Path(out)
    for path in (main, staged_path):
        if not path.is_file():
            raise LedgerError(f"no such file: {path}")
    try:
        staged = parse_text(staged_path.read_text("utf-8-sig"))
    except UnicodeDecodeError as err:
        raise LedgerError(f"{staged_path} is not UTF-8 text: {err}") from err
    except LedgerError as err:
        raise LedgerError(f"{staged_path}: {err}") from err
    entries, _errors, _options = load(main)  # errors the ledger already has do not stop a match
    items = match(entries, staged, settings)
    plan = {
        "version": 1,
        "ledger": str(main),
        "staged": str(staged_path),
        "applied": False,
        "items": [asdict(item) for item in items],
        "balances": [format_entry(e).strip() for e in staged if isinstance(e, Balance)],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", "utf-8", newline="\n")
    summary = {"plan": str(out), **{kind: 0 for kind in KINDS}}
    flagged = {flag: 0 for flag in FLAGS}
    for item in items:
        summary[item.kind] += 1
        if item.kind in ("new", "uncertain"):
            for flag in item.flags:
                flagged[flag] += 1
    summary["flagged"] = flagged
    return summary

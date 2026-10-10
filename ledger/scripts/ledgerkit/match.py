"""Match staged entries against the ledger: each one is new, a duplicate, a fix or uncertain.

Staged transactions are taken in date order and get the ids s1, s2, ...; every staged posting needs
an amount. An existing entry serves at most one duplicate or fix. The rules run in passes over the
whole batch, the surest first, so that a weaker match never takes an entry a surer one needs.

A staged entry dated before `[ledger] start` is not imported: it is a duplicate with reason
`before-start` and takes part in no rule. For each other staged entry S the first rule that
applies wins:

1. Identity.
   - order-id: an existing entry from the same bill (its `bill`, else its `via`) with the same
     `orderId`, or an earlier staged entry with both. With a signed funding amount in common S is a
     duplicate; without one (a refund under its purchase's order id) S is uncertain.
   - statement-row: for a bank line, an existing entry with the same `via` and date that holds every
     non-placeholder posting of S, and the same `balances` when both have one, is a duplicate.
2. chat: an open hand entry in the window with S's signed funding amount on the same account, or,
   when its `channel` is S's source, on that channel's account (`[chat.channels]`, else
   `[chat] default`). The nearest one is fixed to S's account and gets S's order id; several at
   the same distance make S uncertain. A loan, reimbursement or transfer side of the entry never
   pairs through the channel, and no fix books an account twice in one entry.
3. posting: entries in the window that share a posting (account and amount, placeholders aside)
   with S are its candidates. Across the batch, bank lines that name a payment channel claim theirs
   first, then bank lines claim candidates that are transfers when no other candidate is as near;
   each time nearest first, then in batch and file order. A claim makes S a duplicate. After that,
   a staged entry that still has unclaimed candidates is uncertain and lists only those: a platform
   row is never a bank's echo, and another bank line may be a payment of its own.
4. A bank line that names a payment channel and matched nothing is uncertain.
5. An open hand entry in the window whose signed funding amount is within max(2, 10 %) of S's, on
   any account, makes S uncertain.
6. Otherwise S is new.

An open hand entry is one the user told about in chat or added in Fava or by hand: its `via` is
missing or "chat" and no bill has given it an `orderId`. Pad entries and opening balances (an entry
with a posting on an `Equity:` account) pay for nothing and are never candidates of any rule.

A staged entry's placeholder funding postings stand in for its funding account (rules 1, 2 and 5)
only when it has no other: a receipt's or refund's `Assets:Unmapped` is the payer, not an account
of the user's.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from beancount.core.amount import Amount
from beancount.core.data import Balance, Posting, Transaction
from beancount.core.flags import FLAG_PADDING

from .book import (
    CHAT,
    PLACEHOLDERS,
    UNMAPPED,
    UNSORTED_EXPENSE,
    UNSORTED_INCOME,
    digest,
    format_entry,
    funding_postings,
    is_transfer,
    load,
    parse_text,
    transactions,
)
from .settings import LedgerError, Settings, source

EQUITY = "Equity:"
FLOW_ROOTS = ("Expenses:", "Income:")
KINDS = ("new", "duplicate", "fix", "uncertain")
FLAGS = ("unsorted", "unmapped", "investment")
# The staged keys a fix copies onto the hand entry; `via` goes there as `bill`.
FIX_KEYS = ("orderId", "payTime")
# How far a hand entry's amount may be from a bill's and still be asked about (rule 5).
NEAR_ABSOLUTE = Decimal(2)
NEAR_RELATIVE = Decimal("0.1")


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
    """The bill an existing entry's `orderId` came from: `bill` on a fixed hand entry, or `via`."""
    return entry.meta.get("bill") or entry.meta.get("via")


def _order_key(origin, entry: Transaction) -> tuple | None:
    order = entry.meta.get("orderId")
    return None if order is None else (origin, str(order))


def _pays_nothing(entry: Transaction) -> bool:
    """Made by a `pad` directive, or opening balances (a posting on an `Equity:` account)."""
    return entry.flag == FLAG_PADDING or any(p.account.startswith(EQUITY) for p in entry.postings)


def _is_open_hand(entry: Transaction) -> bool:
    """A hand entry that no bill has claimed yet: told in chat, or added in Fava or by hand.

    Its `via` is missing or "chat" and it has no `orderId`; opening balances and pad entries are
    none.
    """
    return (
        entry.meta.get("via", CHAT) == CHAT and "orderId" not in entry.meta
        and not _pays_nothing(entry)
    )


def _other_order(entry: Transaction, staged: Transaction) -> bool:
    """Both carry an order id from the same bill and the ids differ: two different payments."""
    ours, theirs = entry.meta.get("orderId"), staged.meta.get("orderId")
    return (
        _bill(entry) == staged.meta["via"] and ours is not None and theirs is not None
        and str(ours) != str(theirs)
    )


def _funding_amounts(txn: Transaction) -> list[tuple[Decimal, str]]:
    return [units for p in funding_postings(txn) if (units := _units(p)) is not None]


def _staged_funding(staged: Transaction) -> list[Posting]:
    """S's funding postings; its placeholder ones only when it has no other."""
    postings = funding_postings(staged)
    return [p for p in postings if p.account not in PLACEHOLDERS] or postings


def _payments(txn: Transaction, staged: bool) -> set[tuple[Decimal, str]]:
    """The signed funding amounts of an entry; of a staged one, those of `_staged_funding`."""
    postings = _staged_funding(txn) if staged else funding_postings(txn)
    return {units for p in postings if (units := _units(p)) is not None}


def _same_row(entry: Transaction, staged: Transaction) -> bool:
    """An existing entry that is the same bank statement line, imported before.

    The ledger copy holds every non-placeholder posting of the staged line; where the staged line
    has a placeholder the ledger holds what the user put there instead.
    """
    if entry.date != staged.date or entry.meta.get("via") != staged.meta["via"]:
        return False
    if _other_order(entry, staged):
        return False
    if (
        "balances" in entry.meta and "balances" in staged.meta
        and str(entry.meta["balances"]) != str(staged.meta["balances"])
    ):
        return False
    ours = Counter((p.account, _units(p)) for p in entry.postings)
    theirs = Counter((p.account, _units(p)) for p in staged.postings
                     if p.account not in PLACEHOLDERS)
    return bool(theirs) and not theirs - ours


def _shares_posting(entry: Transaction, staged: Transaction) -> bool:
    keys = {(p.account, _units(p)) for p in staged.postings if p.account not in PLACEHOLDERS}
    return any((p.account, _units(p)) in keys for p in entry.postings)


def _target(ours: Posting, theirs: Posting) -> str:
    """The account a fix gives the hand entry's posting: the bill's, unless the bill does not
    know it (a placeholder); the hand entry's account then stays."""
    return ours.account if theirs.account in PLACEHOLDERS else theirs.account


def _books_twice(entry: Transaction, ours: Posting, account: str) -> bool:
    """`account` is on another posting of the entry: moving `ours` there makes nonsense of it."""
    return any(p.account == account for p in entry.postings if p is not ours)


def _chat_pair(
        entry: Transaction, staged: Transaction, settings: Settings, bank: bool,
) -> tuple[Posting, Posting] | None:
    """The funding postings that make a hand entry and a staged entry the same payment, if any.

    The two postings have the same signed amount, and one of these holds:
    - the entry's `channel` is S's source, and its posting is on that channel's account
      (`[chat.channels]`, else `[chat] default`) or on S's account;
    - S is a bank line (`bank`) and both postings are on the same account: the account's own
      statement.
    A platform row never pairs with an entry that names another channel or none: the user said
    one way of paying, the bill shows another. A pair whose fix would book an account twice in the
    entry is none.
    """
    channel = entry.meta.get("channel")
    named = channel is not None and channel == staged.meta["via"]
    through = (settings.chat_channels.get(channel) or settings.chat_default) if named else None
    same, channelled = [], []
    for ours in funding_postings(entry):
        for theirs in _staged_funding(staged):
            if _units(ours) is None or _units(ours) != _units(theirs):
                continue
            if (
                (bank or named) and ours.account == theirs.account
                and ours.account not in PLACEHOLDERS
            ):
                same.append((ours, theirs))
            elif ours.account == through:
                channelled.append((ours, theirs))
    for ours, theirs in same + channelled:
        if not _books_twice(entry, ours, _target(ours, theirs)):
            return ours, theirs
    return None


def _shape(txn: Transaction) -> str:
    """What an entry does with the money: "transfer", "flow" or "unknown".

    A transfer only moves money between the user's own accounts (receivables and payables count),
    none of them a placeholder. A flow has an `Expenses:` or `Income:` posting, placeholders
    included. Anything else, such as a receipt booked against `Assets:Unmapped`, is unknown.
    """
    if any(p.account.startswith(FLOW_ROOTS) for p in txn.postings):
        return "flow"
    if is_transfer(txn) and not any(p.account in PLACEHOLDERS for p in txn.postings):
        return "transfer"
    return "unknown"


def _categories(txn: Transaction) -> set[str]:
    """The `Expenses:` and `Income:` accounts of an entry, placeholders aside."""
    return {p.account for p in txn.postings
            if p.account.startswith(FLOW_ROOTS) and p.account not in PLACEHOLDERS}


def _same_kind(entry: Transaction, staged: Transaction) -> bool:
    """Both are transfers or both are flows, and where both name categories, the same ones.

    A fix keeps the entry's category; one the bill contradicts is the user's to settle.
    """
    shape = _shape(entry)
    if shape == "unknown" or shape != _shape(staged):
        return False
    ours, theirs = _categories(entry), _categories(staged)
    return not (ours and theirs and ours != theirs)


def _near_amount(entry: Transaction, staged: Transaction) -> bool:
    """A signed funding amount within max(2, 10 % of the staged one) of S's, on any account."""
    for number, currency in _funding_amounts(entry):
        for posting in _staged_funding(staged):
            theirs, their_currency = _units(posting)
            limit = max(NEAR_ABSOLUTE, abs(theirs) * NEAR_RELATIVE)
            if currency == their_currency and abs(number - theirs) <= limit:
                return True
    return False


def _location(entry: Transaction) -> tuple[str, int]:
    """The file and header line of an entry, from beancount's metadata."""
    filename = entry.meta.get("filename") or ""
    return (str(Path(filename)) if filename else ""), int(entry.meta.get("lineno") or 0)


def _fix(entry: Transaction, ours: Posting, theirs: Posting, staged: Transaction) -> dict:
    file, line = _location(entry)
    meta = {key: str(staged.meta[key]) for key in FIX_KEYS if key in staged.meta}
    meta["bill"] = staged.meta["via"]
    return {"file": file, "line": line, "account_from": ours.account,
            "account_to": _target(ours, theirs), "meta": meta}


def _start(settings: Settings) -> date | None:
    """The first day of the books, when it is set."""
    return date.fromisoformat(settings.start) if settings.start else None


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


class _Matcher:
    """One batch against the ledger. Entries are referred to by index: `n` staged, `i` existing."""

    def __init__(self, existing: list, staged: list, settings: Settings):
        self.settings = settings
        # Pad entries (in no file) and opening balances pay for nothing: never candidates.
        self.ledger = [t for t in transactions(existing) if not _pays_nothing(t)]
        self.batch = sorted(transactions(staged), key=lambda t: t.date)
        for txn in self.batch:
            if "via" not in txn.meta:
                raise LedgerError(f"staged entry on {txn.date} has no via")
            if any(_units(p) is None for p in txn.postings):
                raise LedgerError(f"staged entry on {txn.date} has a posting without an amount")
        self.bank = [source(settings, t.meta["via"]).kind == "bank" for t in self.batch]
        self.channel = [
            bank and _mentions(txn, settings.channel_keywords)
            for bank, txn in zip(self.bank, self.batch, strict=True)
        ]
        self.used: set[int] = set()
        self.decided: dict[int, Decision] = {}

    def items(self) -> list[Item]:
        self.before_start()
        self.identity()
        self.chats()
        self.claims()
        self.rest()
        items = []
        for n, txn in enumerate(self.batch):
            kind, reason, found, fix = self.decided[n]
            items.append(Item(
                id=f"s{n + 1}", kind=kind, reason=reason, date=txn.date.isoformat(),
                text=format_entry(txn), flags=_flags(txn, self.settings),
                existing=[_ref(entry) for entry in found], fix=fix))
        return items

    def undecided(self) -> list[int]:
        return [n for n in range(len(self.batch)) if n not in self.decided]

    def near(self, staged: Transaction) -> list[int]:
        """The unused existing entries in the window, in file order."""
        found = [i for i, entry in enumerate(self.ledger)
                 if i not in self.used and _days(entry, staged) <= self.settings.window_days]
        return sorted(found, key=lambda i: _location(self.ledger[i]))

    def nearest(self, found: list[int], staged: Transaction) -> list[int]:
        """The entries of `found` at the smallest date distance, in file order."""
        if not found:
            return []
        best = min(_days(self.ledger[i], staged) for i in found)
        return [i for i in found if _days(self.ledger[i], staged) == best]

    def candidates(self, staged: Transaction) -> list[int]:
        """Rule 3: unused entries in the window sharing a posting with S, in file order."""
        return [i for i in self.near(staged)
                if not _other_order(self.ledger[i], staged)
                and _shares_posting(self.ledger[i], staged)]

    def take(self, n: int, i: int, kind: str, reason: str) -> None:
        self.used.add(i)
        self.decided[n] = kind, reason, [self.ledger[i]], None

    def before_start(self) -> None:
        """Rows from before the books' start are not imported."""
        start = _start(self.settings)
        for n, txn in enumerate(self.batch):
            if start is not None and txn.date < start:
                self.decided[n] = "duplicate", "before-start", [], None

    def identity(self) -> None:
        """Rule 1: order ids first, then bank lines imported before."""
        by_order = defaultdict(list)
        for i, entry in enumerate(self.ledger):
            key = _order_key(_bill(entry), entry)
            if key is not None:
                by_order[key].append(i)
        seen = defaultdict(list)  # order key -> the staged entries with it so far
        for n in self.undecided():
            txn = self.batch[n]
            key = _order_key(txn.meta["via"], txn)
            if key is None:
                continue
            # The same order also needs the same money moving the same way: a refund can carry its
            # purchase's order id.
            ours = _payments(txn, staged=True)
            unused = [i for i in by_order[key] if i not in self.used]
            same = [i for i in unused if ours & _payments(self.ledger[i], staged=False)]
            if same:
                self.take(n, same[0], "duplicate", "order-id")
            elif any(ours & _payments(earlier, staged=True) for earlier in seen[key]):
                # Twice in this batch; the first one is decided on its own.
                self.decided[n] = "duplicate", "order-id", [], None
            elif unused or seen[key]:
                found = [self.ledger[i] for i in unused]
                self.decided[n] = "uncertain", "order-id-mismatch", found, None
            seen[key].append(txn)
        for n in self.undecided():
            if self.bank[n]:
                txn = self.batch[n]
                rows = [i for i in self.near(txn) if _same_row(self.ledger[i], txn)]
                if rows:
                    self.take(n, rows[0], "duplicate", "statement-row")

    def chats(self) -> None:
        """Rule 2, in date order."""
        for n in self.undecided():
            txn = self.batch[n]
            pairs = {}
            for i in self.near(txn):
                if _is_open_hand(self.ledger[i]) and _same_kind(self.ledger[i], txn):
                    pair = _chat_pair(self.ledger[i], txn, self.settings, self.bank[n])
                    if pair:
                        pairs[i] = pair
            chats = self.nearest(list(pairs), txn)
            if len(chats) == 1:
                [i] = chats
                self.used.add(i)
                self.decided[n] = "fix", "chat", [self.ledger[i]], _fix(
                    self.ledger[i], *pairs[i], txn)
            elif chats:
                self.decided[n] = "uncertain", "chat", [self.ledger[i] for i in chats], None

    def claims(self) -> None:
        """Rule 3 for bank lines: channel lines claim candidates, then lines claim transfers.

        Each round goes over the whole batch nearest first, then in batch and file order. A line
        claims a transfer only when it has no other candidate as near as that transfer.
        """
        for reason in ("channel", "transfer"):
            pairs = []
            for n in self.undecided():
                if not self.bank[n] or (reason == "channel" and not self.channel[n]):
                    continue
                txn = self.batch[n]
                found = self.candidates(txn)
                # A transfer is claimed only when no other candidate is as near.
                others = [_days(self.ledger[i], txn) for i in found
                          if not is_transfer(self.ledger[i])]
                limit = min(others, default=None)
                for i in found:
                    days = _days(self.ledger[i], txn)
                    if reason == "channel" or (
                            is_transfer(self.ledger[i]) and (limit is None or days < limit)):
                        pairs.append((days, n, _location(self.ledger[i]), i))
            for _days_apart, n, _where, i in sorted(pairs):
                if n not in self.decided and i not in self.used:
                    self.take(n, i, "duplicate", reason)

    def rest(self) -> None:
        """Rule 3 for what no claim settled: candidates nobody claimed; then rules 4 to 6."""
        for n in self.undecided():
            txn = self.batch[n]
            tied = self.nearest(self.candidates(txn), txn)
            if tied:
                self.decided[n] = (
                    "uncertain", "possible-duplicate", [self.ledger[i] for i in tied], None)
                continue
            if self.channel[n]:
                self.decided[n] = "uncertain", "channel-unmatched", [], None
                continue
            chats = self.nearest(
                [i for i in self.near(txn)
                 if _is_open_hand(self.ledger[i]) and _near_amount(self.ledger[i], txn)],
                txn)
            if chats:
                self.decided[n] = (
                    "uncertain", "possible-chat", [self.ledger[i] for i in chats], None)
            else:
                self.decided[n] = "new", "new", [], None


def match(existing: list, staged: list, settings: Settings) -> list[Item]:
    """Decide every staged transaction against the existing entries. See the module doc."""
    return _Matcher(existing, staged, settings).items()


def run(main: Path, staged_path: Path, out: Path, settings: Settings) -> dict:
    """Match the staged file against the ledger and write the plan to `out`. Returns a summary.

    The flag counts cover the new and uncertain items, the ones that may be written: a duplicate
    is dropped and a fix keeps the existing entry's postings apart from the funding account.
    `ledger_errors` counts the errors the ledger already had; they do not stop a match.
    The plan holds the ledger's digest, taken before it is read: apply refuses the plan once the
    ledger has changed.
    Balances dated on or before `[ledger] start` are left out: they check the money before the
    books begin, which the opening balances, dated that first day, do not count yet.
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
    ledger_digest = digest(main.parent)
    entries, errors, _options = load(main)
    items = match(entries, staged, settings)
    start = _start(settings)
    plan = {
        "version": 1,
        "ledger": str(main.absolute()),
        "ledger_digest": ledger_digest,
        "staged": str(staged_path),
        "applied": False,
        "items": [asdict(item) for item in items],
        "balances": [
            format_entry(e).strip() for e in staged
            if isinstance(e, Balance) and (start is None or e.date > start)],
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
    summary["ledger_errors"] = len(errors)
    return summary

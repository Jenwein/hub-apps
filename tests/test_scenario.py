"""One month of the ledger, end to end: chat entries, a WeChat bill, a CCB statement, and the
WeChat bill again, through `match.run` and `apply.apply_plan` as the skill runs them."""

import json
from collections import Counter
from decimal import Decimal

from beancount.core.data import Transaction
from ledgerkit.apply import apply_plan
from ledgerkit.book import add_text, load
from ledgerkit.match import run
from ledgerkit.settings import load_settings

CARD = "Assets:Bank:CCB:1234"
CHANGE = "Assets:WeChat:Change"
LOAN = "Assets:Receivables:Loans:ZhangSan"

ACCOUNTS = f"2026-09-01 open {CARD}\n2026-09-01 open {CHANGE}\n2026-09-01 open {LOAN}\n"
OPENING = (
    '2026-09-01 * "Opening balances"\n'
    f"  {CARD}  5000.00 CNY\n  {CHANGE}  200.00 CNY\n  Equity:Opening-Balances\n"
)
# What the user said in chat; WeChat Pay usually draws from the card ([chat.channels]).
CHAT = (
    '2026-09-05 * "食堂" "lunch"\n  channel: "wechat"\n'
    f"  Expenses:Food  35.00 CNY\n  {CARD}  -35.00 CNY\n\n"
    '2026-09-06 * "张三" "lent Zhang San"\n  channel: "wechat"\n'
    f"  {LOAN}  500.00 CNY\n  {CARD}  -500.00 CNY\n"
)
WECHAT_BILL = (
    # Before the books start.
    '2026-08-30 * "超市" "购物"\n  via: "wechat"\n  orderId: "W0"\n'
    f"  Expenses:Groceries  12.00 CNY\n  {CHANGE}  -12.00 CNY\n\n"
    # A top-up from the card; the CCB statement shows it again.
    '2026-09-03 * "零钱" "零钱充值"\n  via: "wechat"\n  orderId: "W1"\n'
    f"  {CHANGE}  100.00 CNY\n  {CARD}  -100.00 CNY\n\n"
    # The lunch, paid from 零钱 rather than the card.
    '2026-09-05 * "食堂" "午餐"\n  via: "wechat"\n  orderId: "W2"\n'
    '  payTime: "2026-09-05 12:03:00 +0800 CST"\n'
    f"  Expenses:Unsorted  35.00 CNY\n  {CHANGE}  -35.00 CNY\n\n"
    # Money in from someone else, the same amount as the loan: not the loan.
    '2026-09-05 * "李四" "转账"\n  via: "wechat"\n  orderId: "W3"\n'
    f"  {CHANGE}  500.00 CNY\n  Income:Gifts  -500.00 CNY\n"
)
CCB_STATEMENT = (
    '2026-09-03 * "财付通" "财付通-微信零钱充值"\n  via: "ccb"\n'
    f"  Expenses:Unsorted  100.00 CNY\n  {CARD}  -100.00 CNY\n\n"
    '2026-09-08 * "某超市" "消费"\n  via: "ccb"\n'
    f"  Expenses:Groceries  58.20 CNY\n  {CARD}  -58.20 CNY\n"
)


def set_up(make_ledger):
    d = make_ledger(ACCOUNTS)
    (d / "txns" / "opening.beancount").write_text(OPENING, "utf-8", newline="\n")
    config = d / "config.toml"
    text = config.read_text("utf-8").replace('start = ""', 'start = "2026-09-01"', 1)
    lines = text.splitlines(keepends=True)
    at = next(n for n, line in enumerate(lines) if line.startswith("[chat.channels]"))
    lines.insert(at + 1, f'wechat = "{CARD}"\n')
    config.write_text("".join(lines), "utf-8", newline="\n")
    return d


def match(d, folder, staged_text):
    """Stage a bill in its inbox folder and match it: (plan path, plan)."""
    staged = d / "inbox" / folder / "staged.beancount"
    staged.parent.mkdir(parents=True)
    staged.write_text(staged_text, "utf-8", newline="\n")
    plan = staged.with_name("plan.json")
    run(d / "main.beancount", staged, plan, load_settings(d))
    return plan, json.loads(plan.read_text("utf-8"))


def decided(plan):
    return [(item["kind"], item["reason"]) for item in plan["items"]]


def ledger_state(d):
    entries, errors, _ = load(d / "main.beancount")
    txns = [e for e in entries if isinstance(e, Transaction)]
    balances = Counter()
    for txn in txns:
        for posting in txn.postings:
            balances[posting.account] += posting.units.number
    return txns, errors, balances


def test_a_month_of_bills(make_ledger):
    d = set_up(make_ledger)
    assert load_settings(d).chat_channels == {"wechat": CARD}
    assert add_text(d, CHAT)["written"] == 2

    # The WeChat bill.
    plan_path, plan = match(d, "20261001-090000-wechat", WECHAT_BILL)
    assert decided(plan) == [
        ("duplicate", "before-start"), ("new", "new"), ("fix", "chat"),
        ("uncertain", "possible-chat")]
    lunch_fix = plan["items"][2]["fix"]
    assert (lunch_fix["account_from"], lunch_fix["account_to"]) == (CARD, CHANGE)
    receipt = plan["items"][3]
    assert "lent Zhang San" in receipt["existing"][0]["text"]
    # The user says the receipt is not the loan.
    result = apply_plan(plan_path, d, {receipt["id"]: "new"})
    assert (result["written"], result["skipped"], result["fixed"]) == (2, 1, 1)

    # The CCB statement: the top-up's echo and a card payment.
    plan_path, plan = match(d, "20261001-091000-ccb", CCB_STATEMENT)
    assert decided(plan) == [("duplicate", "channel"), ("new", "new")]
    result = apply_plan(plan_path, d, {})
    assert (result["written"], result["skipped"], result["fixed"]) == (1, 1, 0)

    # The WeChat bill sent again, and the statement too.
    plan_path, plan = match(d, "20261001-092000-wechat", WECHAT_BILL)
    assert decided(plan) == [
        ("duplicate", "before-start")] + [("duplicate", "order-id")] * 3
    assert apply_plan(plan_path, d, {}) == {
        "written": 0, "skipped": 4, "fixed": 0, "balances": 0, "document": None}
    plan_path, plan = match(d, "20261001-093000-ccb", CCB_STATEMENT)
    assert decided(plan) == [("duplicate", "channel"), ("duplicate", "statement-row")]
    apply_plan(plan_path, d, {})

    txns, errors, balances = ledger_state(d)
    assert errors == []  # bean-check is clean
    # Each payment once: the opening balances, the two chat entries, the top-up, the receipt and
    # the card payment.
    assert sorted(t.narration for t in txns) == sorted([
        "Opening balances", "lunch", "lent Zhang San", "零钱充值", "转账", "消费"])
    orders = [str(t.meta["orderId"]) for t in txns if "orderId" in t.meta]
    assert sorted(orders) == ["W1", "W2", "W3"]
    assert balances[CARD] == Decimal("5000.00") - 100 - 500 - Decimal("58.20")
    assert balances[CHANGE] == Decimal("200.00") + 100 - 35 + 500
    assert balances[LOAN] == 500
    [loan] = [t for t in txns if t.narration == "lent Zhang San"]
    assert [(p.account, p.units.number) for p in loan.postings] == [(LOAN, 500), (CARD, -500)]
    assert "orderId" not in loan.meta and loan.meta["via"] == "chat"
    [lunch] = [t for t in txns if t.narration == "lunch"]
    assert [(p.account, p.units.number) for p in lunch.postings] == [
        ("Expenses:Food", 35), (CHANGE, -35)]
    assert (lunch.meta["orderId"], lunch.meta["bill"]) == ("W2", "wechat")
    assert not any(t.date.isoformat() < "2026-09-01" for t in txns)

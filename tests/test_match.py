import json
import re

import pytest
from ledgerkit.book import digest, format_entry, parse_text
from ledgerkit.cli import main
from ledgerkit.match import match, run
from ledgerkit.settings import LedgerError, parse_settings

CARD = "Assets:Bank:CCB:1234"
# The chat entries below that name a channel are on the account the user's settings give it.
SET = parse_settings({"chat": {"channels": {"wechat": CARD, "alipay": CARD}}})
ALIPAY_RIDE = '2026-09-03 * "高德打车" "打车"\n  via: "alipay"\n  orderId: "A1"\n  Expenses:Transport  20.00 CNY\n  Assets:Bank:CCB:1234  -20.00 CNY\n'
BANK_RIDE = '2026-09-04 * "高***公司" "支付宝-支付宝外部商户-高德打车"\n  via: "ccb"\n  Expenses:Unsorted  20.00 CNY\n  Assets:Bank:CCB:1234  -20.00 CNY\n'
TOPUP = '2026-09-05 * "支付宝" "充值"\n  via: "alipay"\n  orderId: "A2"\n  Assets:Alipay:Balance  100.00 CNY\n  Assets:Bank:CCB:1234  -100.00 CNY\n'
BANK_TOPUP = '2026-09-05 * "支付宝" "支付宝-充值"\n  via: "ccb"\n  Expenses:Unsorted  100.00 CNY\n  Assets:Bank:CCB:1234  -100.00 CNY\n'
CC_PAY = '2026-09-10 * "工行信用卡" "还款"\n  via: "ccb"\n  Liabilities:CreditCard:ICBC:5678  1000.00 CNY\n  Assets:Bank:CCB:1234  -1000.00 CNY\n'
CC_SIDE = '2026-09-11 * "" "还款"\n  via: "icbc"\n  Liabilities:CreditCard:ICBC:5678  1000.00 CNY\n  Income:Unsorted  -1000.00 CNY\n'
CHAT_LUNCH = '2026-09-06 * "" "lunch"\n  via: "chat"\n  channel: "wechat"\n  Expenses:Food  35.00 CNY\n  Assets:Bank:CCB:1234  -35.00 CNY\n'
WX_LUNCH = '2026-09-06 * "食堂" "午餐"\n  via: "wechat"\n  orderId: "W1"\n  payTime: "2026-09-06 12:01:02 +0800 CST"\n  Expenses:Unsorted  35.00 CNY\n  Assets:WeChat:Change  -35.00 CNY\n'

ITEM_KEYS = {"id", "kind", "reason", "date", "text", "flags", "existing", "fix"}


def run_match(existing, staged):
    return match(parse_text(existing), parse_text(staged), SET)


def kinds(items):
    return [(i.kind, i.reason) for i in items]


def test_new_entry():
    assert kinds(run_match("", ALIPAY_RIDE)) == [("new", "new")]


def test_reimport_is_all_duplicates():
    assert kinds(run_match(ALIPAY_RIDE + TOPUP, ALIPAY_RIDE + TOPUP)) == [("duplicate", "order-id")] * 2


def test_bank_channel_line_is_duplicate():
    assert kinds(run_match(ALIPAY_RIDE, BANK_RIDE)) == [("duplicate", "channel")]


def test_channel_line_without_platform_entry():
    assert kinds(run_match("", BANK_RIDE)) == [("uncertain", "channel-unmatched")]


def test_topup_recorded_once():
    assert kinds(run_match(TOPUP, BANK_TOPUP)) == [("duplicate", "channel")]


def test_credit_card_repayment_is_transfer():
    assert kinds(run_match(CC_PAY, CC_SIDE)) == [("duplicate", "transfer")]


def test_chat_entry_gets_fixed():
    [item] = run_match(CHAT_LUNCH, WX_LUNCH)
    assert (item.kind, item.reason) == ("fix", "chat")
    assert item.fix["account_from"] == "Assets:Bank:CCB:1234"
    assert item.fix["account_to"] == "Assets:WeChat:Change"
    assert item.fix["meta"] == {"orderId": "W1", "payTime": "2026-09-06 12:01:02 +0800 CST", "bill": "wechat"}


def test_two_chat_candidates_are_uncertain():
    assert kinds(run_match(CHAT_LUNCH + CHAT_LUNCH.replace("lunch", "snack"), WX_LUNCH)) == [("uncertain", "chat")]


def test_chat_without_channel_on_other_account_is_uncertain():
    chat = CHAT_LUNCH.replace('  channel: "wechat"\n', "").replace("Assets:Bank:CCB:1234", "Assets:Cash")
    assert kinds(run_match(chat, WX_LUNCH)) == [("uncertain", "possible-chat")]


def test_same_amount_ordinary_bank_line_is_uncertain():
    shop = BANK_RIDE.replace("支付宝-支付宝外部商户-高德打车", "某超市")
    assert kinds(run_match(ALIPAY_RIDE, shop)) == [("uncertain", "possible-duplicate")]


def test_outside_window_is_not_matched():
    assert kinds(run_match(ALIPAY_RIDE, BANK_RIDE.replace("2026-09-04", "2026-09-08"))) == [("uncertain", "channel-unmatched")]


def test_staged_entry_without_via_is_an_error():
    with pytest.raises(LedgerError, match="via"):
        run_match("", ALIPAY_RIDE.replace('  via: "alipay"\n', ""))


def test_identical_amounts_match_one_to_one():
    existing = ALIPAY_RIDE + ALIPAY_RIDE.replace('"A1"', '"A3"')
    assert kinds(run_match(existing, BANK_RIDE + BANK_RIDE)) == [("duplicate", "channel")] * 2
    assert kinds(run_match(ALIPAY_RIDE, BANK_RIDE + BANK_RIDE)) == [("duplicate", "channel"), ("uncertain", "channel-unmatched")]


def test_flags():
    fund = '2026-09-07 * "蚂蚁财富" "基金申购"\n  via: "alipay"\n  orderId: "A9"\n  Expenses:Unsorted  500.00 CNY\n  Assets:Unmapped  -500.00 CNY\n'
    [item] = run_match("", fund)
    assert set(item.flags) == {"unsorted", "unmapped", "investment"}


def test_run_writes_plan(make_ledger, tmp_path):
    d = make_ledger("2000-01-01 open Assets:Bank:CCB:1234\n", txns={2026: ALIPAY_RIDE})
    staged = tmp_path / "s.beancount"
    staged.write_text(BANK_RIDE + "2026-10-01 balance Assets:Bank:CCB:1234  2140.13 CNY\n", "utf-8")
    summary = run(d / "main.beancount", staged, tmp_path / "plan.json", SET)
    plan = json.loads((tmp_path / "plan.json").read_text("utf-8"))
    assert summary["duplicate"] == 1 and plan["applied"] is False
    assert plan["items"][0]["existing"][0]["line"] > 0
    # The printer pads the number to a column.
    [balance] = plan["balances"]
    assert " ".join(balance.split()) == "2026-10-01 balance Assets:Bank:CCB:1234 2140.13 CNY"


# Controller rulings on rule 3 (posting).

WX_TOPUP = (
    '2026-09-05 * "零钱" "零钱充值"\n  via: "wechat"\n  orderId: "W7"\n'
    "  Assets:WeChat:Change  100.00 CNY\n  Assets:Bank:CCB:1234  -100.00 CNY\n"
)


def test_platform_entry_is_never_a_posting_duplicate():
    # TOPUP is a transfer and shares the bank posting, but a platform row is no bank echo.
    assert kinds(run_match(TOPUP, WX_TOPUP)) == [("uncertain", "possible-duplicate")]


def test_other_order_id_from_the_same_source_is_new():
    assert kinds(run_match(ALIPAY_RIDE, ALIPAY_RIDE.replace('"A1"', '"A5"'))) == [("new", "new")]


def test_placeholder_postings_are_not_shared():
    shop = BANK_RIDE.replace("支付宝-支付宝外部商户-高德打车", "某超市")
    existing = shop.replace("Assets:Bank:CCB:1234", "Assets:Cash")  # shares only Expenses:Unsorted
    assert kinds(run_match(existing, shop)) == [("new", "new")]


# Beyond the brief.


def test_order_id_twice_in_the_batch():
    items = run_match("", ALIPAY_RIDE + ALIPAY_RIDE)
    assert kinds(items) == [("new", "new"), ("duplicate", "order-id")]
    assert items[1].existing == []


def test_fixed_chat_entry_matches_its_bill_order_id():
    claimed = '  channel: "wechat"\n  orderId: "W1"\n  bill: "wechat"\n'
    fixed = CHAT_LUNCH.replace('  channel: "wechat"\n', claimed)
    fixed = fixed.replace("Assets:Bank:CCB:1234", "Assets:WeChat:Change")
    assert kinds(run_match(fixed, WX_LUNCH)) == [("duplicate", "order-id")]


def test_order_id_match_is_not_taken_by_an_earlier_row():
    paid = CC_PAY.replace('  via: "ccb"\n', '  via: "ccb"\n  orderId: "C1"\n')
    earlier = CC_PAY.replace("2026-09-10", "2026-09-09")
    # Without the order-id pass first, the 09-09 row would take `paid` as a transfer and the
    # 09-10 row, the very same entry, would come out new.
    assert kinds(run_match(paid, earlier + paid)) == [("new", "new"), ("duplicate", "order-id")]


def test_fix_never_moves_to_a_placeholder():
    [item] = run_match(CHAT_LUNCH, WX_LUNCH.replace("Assets:WeChat:Change", "Assets:Unmapped"))
    assert (item.kind, item.reason) == ("fix", "chat")
    assert item.fix["account_from"] == item.fix["account_to"] == "Assets:Bank:CCB:1234"
    assert "unmapped" in item.flags


def test_chat_on_the_same_account_keeps_it():
    [item] = run_match(CHAT_LUNCH, WX_LUNCH.replace("Assets:WeChat:Change", "Assets:Bank:CCB:1234"))
    assert (item.kind, item.reason) == ("fix", "chat")
    assert item.fix["account_from"] == item.fix["account_to"] == "Assets:Bank:CCB:1234"


def test_nearest_chat_entry_wins():
    far = CHAT_LUNCH.replace("2026-09-06", "2026-09-08").replace("lunch", "snack")
    [item] = run_match(far + CHAT_LUNCH, WX_LUNCH)
    assert (item.kind, item.reason) == ("fix", "chat")
    assert '"lunch"' in item.existing[0]["text"]


def test_item_fields():
    [item] = run_match(CHAT_LUNCH, WX_LUNCH)
    assert set(vars(item)) == ITEM_KEYS
    assert item.id == "s1" and item.date == "2026-09-06"
    assert item.text == format_entry(parse_text(WX_LUNCH)[0])
    assert item.existing == [
        {"file": "<string>", "line": 1, "text": format_entry(parse_text(CHAT_LUNCH)[0])}]
    assert (item.fix["file"], item.fix["line"]) == ("<string>", 1)


def test_new_item_has_no_existing_and_no_fix():
    [item] = run_match("", ALIPAY_RIDE)
    assert item.existing == [] and item.fix is None and item.flags == []


def test_ids_follow_date_order():
    staged = parse_text(ALIPAY_RIDE + TOPUP)[::-1]  # TOPUP (09-05) first
    items = match([], staged, SET)
    assert [(i.id, i.date) for i in items] == [("s1", "2026-09-03"), ("s2", "2026-09-05")]


def test_unknown_source_is_an_error():
    with pytest.raises(LedgerError, match="unknown source nope"):
        run_match("", ALIPAY_RIDE.replace('"alipay"', '"nope"'))


def test_flag_counts_cover_only_entries_that_may_be_written(make_ledger, tmp_path):
    fund = (
        '2026-09-07 * "蚂蚁财富" "基金申购"\n  via: "alipay"\n  orderId: "A9"\n'
        "  Expenses:Unsorted  500.00 CNY\n  Assets:Unmapped  -500.00 CNY\n"
    )
    d = make_ledger("2000-01-01 open Assets:Bank:CCB:1234\n", txns={2026: ALIPAY_RIDE + CHAT_LUNCH})
    staged = tmp_path / "s.beancount"
    # Every one is unsorted: a duplicate (BANK_RIDE), an uncertain channel line (BANK_TOPUP),
    # a fix (WX_LUNCH, the chat entry keeps its category) and a new one (fund).
    staged.write_text(BANK_RIDE + BANK_TOPUP + WX_LUNCH + fund, "utf-8")
    summary = run(d / "main.beancount", staged, tmp_path / "plan.json", SET)
    assert summary == {
        "plan": str(tmp_path / "plan.json"), "new": 1, "duplicate": 1, "fix": 1, "uncertain": 1,
        "flagged": {"unsorted": 2, "unmapped": 1, "investment": 1}, "ledger_errors": 0,
    }


def test_plan_file(make_ledger, tmp_path):
    d = make_ledger(txns={2026: CHAT_LUNCH})
    staged = tmp_path / "staged 暂存.beancount"
    staged.write_text(WX_LUNCH, "utf-8")
    out = tmp_path / "plans" / "plan.json"
    run(d / "main.beancount", staged, out, SET)
    text = out.read_text("utf-8")
    assert "食堂" in text  # not escaped
    plan = json.loads(text)
    assert plan["version"] == 1 and plan["balances"] == []
    assert plan["ledger"] == str(d / "main.beancount") and plan["staged"] == str(staged)
    [item] = plan["items"]
    assert set(item) == ITEM_KEYS and item["kind"] == "fix"
    assert item["fix"]["file"] == str(d / "txns" / "2026.beancount") and item["fix"]["line"] == 1
    assert item["existing"][0]["file"] == item["fix"]["file"]
    assert plan["ledger_digest"] == digest(d)


def test_missing_staged_file(make_ledger, tmp_path):
    d = make_ledger()
    with pytest.raises(LedgerError, match="no such file"):
        run(d / "main.beancount", tmp_path / "nope.beancount", tmp_path / "plan.json", SET)
    assert not (tmp_path / "plan.json").exists()


def test_cli_match(make_ledger, tmp_path, capsys):
    d = make_ledger(txns={2026: ALIPAY_RIDE})
    staged = tmp_path / "s.beancount"
    staged.write_text(ALIPAY_RIDE, "utf-8")
    out = tmp_path / "plan.json"
    assert main(["match", "--ledger-dir", str(d), "--staged", str(staged), "--out", str(out)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["duplicate"] == 1 and summary["plan"] == str(out) and out.is_file()


# Fix round 1.


def test_staged_posting_without_an_amount_is_an_error():
    # The card amount elided: it would share no posting and come out as something new.
    staged = BANK_RIDE.replace("Assets:Bank:CCB:1234  -20.00 CNY", "Assets:Bank:CCB:1234")
    with pytest.raises(LedgerError, match="2026-09-04 has a posting without an amount"):
        run_match(ALIPAY_RIDE, staged)


TAXI_CHAT = (
    '2026-09-03 * "" "taxi"\n  via: "chat"\n  channel: "alipay"\n'
    "  Expenses:Transport  20.00 CNY\n  Assets:Bank:CCB:1234  -20.00 CNY\n"
)


@pytest.mark.parametrize(("said", "billed", "expected"), [
    ("20.00", "19.80", ("uncertain", "possible-chat")),
    ("20.00", "22.00", ("uncertain", "possible-chat")),  # within 2.00
    ("20.00", "23.00", ("new", "new")),
    ("200.00", "219.00", ("uncertain", "possible-chat")),  # within 10 % of 219.00
    ("200.00", "223.00", ("new", "new")),
])
def test_chat_entry_with_a_near_amount(said, billed, expected):
    chat = TAXI_CHAT.replace("20.00", said)
    assert kinds(run_match(chat, ALIPAY_RIDE.replace("20.00", billed))) == [expected]


def test_transfer_goes_to_the_nearest_bank_row():
    zhang = (
        '2026-09-04 * "张三" "转账"\n  via: "ccb"\n'
        "  Expenses:Unsorted  100.00 CNY\n  Assets:Bank:CCB:1234  -100.00 CNY\n"
    )
    assert kinds(run_match(TOPUP, zhang + BANK_TOPUP)) == [("new", "new"), ("duplicate", "channel")]
    # At the same distance the channel line goes first.
    same_day = zhang.replace("2026-09-04", "2026-09-05")
    assert kinds(run_match(TOPUP, same_day + BANK_TOPUP)) == [
        ("new", "new"), ("duplicate", "channel")]


def _row(day: str, balance: str, category: str, amount: str = "20.00") -> str:
    return (
        f'2026-09-{day} * "某超市" "消费"\n  via: "ccb"\n  balances: "{balance}"\n'
        f"  {category}  {amount} CNY\n  Assets:Bank:CCB:1234  -{amount} CNY\n"
    )


def test_reimported_statement_is_all_duplicates():
    salary = (
        '2026-09-09 * "某公司" "工资"\n  via: "ccb"\n  balances: "5960.00"\n'
        "  Assets:Bank:CCB:1234  5000.00 CNY\n  {other}  -5000.00 CNY\n"
    )
    staged = (_row("08", "980.00", "Expenses:Unsorted") + _row("08", "960.00", "Expenses:Unsorted")
              + salary.format(other="Assets:Unmapped"))
    # As written to the ledger: categories filled in, the two same-day rows the other way round.
    ledger = (_row("08", "960.00", "Expenses:Food") + _row("08", "980.00", "Expenses:Groceries")
              + salary.format(other="Income:Salary"))
    items = run_match(ledger, staged)
    assert kinds(items) == [("duplicate", "statement-row")] * 3
    matched = [re.search(r'balances: "(.*)"', i.existing[0]["text"])[1] for i in items]
    assert matched == ["980.00", "960.00", "5960.00"]


def test_identical_statement_rows_match_one_to_one():
    row = (
        '2026-09-08 * "某超市" "消费"\n  via: "ccb"\n'
        "  Expenses:Food  20.00 CNY\n  Assets:Bank:CCB:1234  -20.00 CNY\n"
    )
    staged = row.replace("Expenses:Food", "Expenses:Unsorted")
    assert kinds(run_match(row + row, staged * 3)) == [("duplicate", "statement-row")] * 2 + [
        ("new", "new")]


JD_BUY = (
    '2026-09-03 * "京东" "卫衣"\n  via: "jd"\n  orderId: "J1"\n'
    "  Expenses:Shopping  131.77 CNY\n  Liabilities:CreditCard:BOC:1875  -131.77 CNY\n"
)
JD_REFUND = (
    '2026-09-05 * "京东" "退款-卫衣"\n  via: "jd"\n  orderId: "J1"\n'
    "  Income:Unsorted  -89.84 CNY\n  Liabilities:CreditCard:BOC:1875  89.84 CNY\n"
)


def test_same_order_id_with_another_amount_is_uncertain():
    [item] = run_match(JD_BUY, JD_REFUND)
    assert (item.kind, item.reason) == ("uncertain", "order-id-mismatch")
    assert "卫衣" in item.existing[0]["text"]
    full_refund = JD_REFUND.replace("89.84", "131.77")
    assert kinds(run_match(JD_BUY, full_refund)) == [("uncertain", "order-id-mismatch")]
    assert kinds(run_match("", JD_BUY + JD_REFUND)) == [
        ("new", "new"), ("uncertain", "order-id-mismatch")]
    assert kinds(run_match(JD_BUY + JD_REFUND, JD_BUY + JD_REFUND)) == [
        ("duplicate", "order-id")] * 2


def test_summary_counts_the_errors_the_ledger_already_has(make_ledger, tmp_path):
    d = make_ledger("2000-01-01 open Assets:Bank:CCB:1234\n", txns={2026: ALIPAY_RIDE})
    staged = tmp_path / "s.beancount"
    staged.write_text(BANK_RIDE, "utf-8")
    assert run(d / "main.beancount", staged, tmp_path / "p.json", SET)["ledger_errors"] == 0
    (d / "txns" / "2025.beancount").write_text(
        ALIPAY_RIDE.replace("2026", "2025").replace("Expenses:Transport", "Expenses:Nope"), "utf-8")
    assert run(d / "main.beancount", staged, tmp_path / "p.json", SET)["ledger_errors"] == 1


def test_pad_entries_are_never_candidates(make_ledger, tmp_path):
    pad = (
        "2026-09-01 pad Assets:Bank:CCB:1234 Equity:Opening-Balances\n"
        "2026-09-04 balance Assets:Bank:CCB:1234  -20.00 CNY\n"
    )
    d = make_ledger("2000-01-01 open Assets:Bank:CCB:1234\n", txns={2026: pad})
    staged = tmp_path / "s.beancount"
    staged.write_text(BANK_RIDE, "utf-8")  # shares Assets:Bank:CCB:1234 -20.00 with the padding
    summary = run(d / "main.beancount", staged, tmp_path / "plan.json", SET)
    assert summary["ledger_errors"] == 0
    [item] = json.loads((tmp_path / "plan.json").read_text("utf-8"))["items"]
    assert (item["kind"], item["reason"]) == ("uncertain", "channel-unmatched")


# Fix round 2.


def assert_no_claimed_entry_listed_again(items):
    """An entry a duplicate or fix took is listed by no other item."""
    claims = [(e["file"], e["line"]) for i in items if i.kind in ("duplicate", "fix")
              for e in i.existing]
    assert len(claims) == len(set(claims))
    for item in items:
        if item.kind == "uncertain":
            assert not set(claims) & {(e["file"], e["line"]) for e in item.existing}


def test_channel_lines_claim_before_nearer_rows():
    zhang = (
        '2026-09-04 * "张三" "转账"\n  via: "ccb"\n'
        "  Expenses:Unsorted  100.00 CNY\n  Assets:Bank:CCB:1234  -100.00 CNY\n"
    )
    topup = TOPUP.replace("2026-09-05", "2026-09-04")  # nearer to 张三 than to the echo
    items = run_match(topup, zhang + BANK_TOPUP)
    assert kinds(items) == [("new", "new"), ("duplicate", "channel")]
    assert_no_claimed_entry_listed_again(items)


def test_row_whose_candidate_was_claimed_is_new():
    shop = (
        '2026-09-03 * "某超市" "购物"\n  via: "ccb"\n'
        "  Expenses:Unsorted  20.00 CNY\n  Assets:Bank:CCB:1234  -20.00 CNY\n"
    )
    items = run_match(ALIPAY_RIDE, shop + BANK_RIDE)
    assert kinds(items) == [("new", "new"), ("duplicate", "channel")]
    assert_no_claimed_entry_listed_again(items)


CARD_REFUND = (  # shares the card posting with CC_SIDE (09-11), but is no transfer
    '2026-09-11 * "某店" "退款"\n  via: "alipay"\n  orderId: "A8"\n'
    "  Liabilities:CreditCard:ICBC:5678  1000.00 CNY\n  Expenses:Shopping  -1000.00 CNY\n"
)


def test_transfer_row_with_a_nearer_other_candidate_is_uncertain():
    items = run_match(CARD_REFUND + CC_PAY.replace("2026-09-10", "2026-09-13"), CC_SIDE)
    assert kinds(items) == [("uncertain", "possible-duplicate")]
    assert ['"退款"' in e["text"] for e in items[0].existing] == [True]


def test_transfer_row_with_an_equally_near_other_candidate_is_uncertain():
    items = run_match(CARD_REFUND + CC_PAY.replace("2026-09-10", "2026-09-11"), CC_SIDE)
    assert kinds(items) == [("uncertain", "possible-duplicate")]
    assert len(items[0].existing) == 2


def test_transfer_row_claims_its_nearest_transfer():
    refund = CARD_REFUND.replace("2026-09-11", "2026-09-13")
    items = run_match(refund + CC_PAY.replace("2026-09-10", "2026-09-11"), CC_SIDE)
    assert kinds(items) == [("duplicate", "transfer")]
    assert '"工行信用卡" "还款"' in items[0].existing[0]["text"]


def test_receipt_never_matches_a_chat_expense():
    # The converter books a receipt against Assets:Unmapped: the payer, not the user's account.
    receipt = (
        '2026-09-03 * "某人" "收款"\n  via: "alipay"\n  orderId: "A7"\n'
        "  Assets:Alipay:Balance  20.00 CNY\n  Assets:Unmapped  -20.00 CNY\n"
    )
    assert kinds(run_match(TAXI_CHAT, receipt)) == [("new", "new")]
    near = receipt.replace("20.00", "19.80")
    assert kinds(run_match(TAXI_CHAT, near)) == [("new", "new")]


# Fix round 3.

# A refund as the converter writes it: the money comes back to the payment method and the other
# side is Assets:Unmapped, whose -X looks like the purchase's payment.
JD_REFUND_CONVERTED = (
    '2026-09-05 * "京东" "退款-卫衣"\n  via: "jd"\n  orderId: "J1"\n'
    "  Liabilities:CreditCard:BOC:1875  131.77 CNY\n  Assets:Unmapped  -131.77 CNY\n"
)
ALIPAY_SHIRT = (
    '2026-09-03 * "某店" "衬衫"\n  via: "alipay"\n  orderId: "A20"\n'
    "  Expenses:Shopping  50.00 CNY\n  Assets:Alipay:Balance  -50.00 CNY\n"
)
ALIPAY_SHIRT_REFUND = (
    '2026-09-05 * "某店" "退款-衬衫"\n  via: "alipay"\n  orderId: "A20"\n'
    "  Assets:Alipay:Balance  50.00 CNY\n  Assets:Unmapped  -50.00 CNY\n"
)


def test_full_refund_in_the_converter_shape_is_not_its_purchase():
    mismatch = [("uncertain", "order-id-mismatch")]
    assert kinds(run_match(JD_BUY, JD_REFUND_CONVERTED)) == mismatch
    assert kinds(run_match("", JD_BUY + JD_REFUND_CONVERTED)) == [("new", "new"), *mismatch]
    assert kinds(run_match(ALIPAY_SHIRT, ALIPAY_SHIRT_REFUND)) == mismatch
    assert kinds(run_match("", ALIPAY_SHIRT + ALIPAY_SHIRT_REFUND)) == [("new", "new"), *mismatch]
    # Same day, the refund first in the file (bills list the newest row first).
    same_day = JD_REFUND_CONVERTED.replace("2026-09-05", "2026-09-03")
    assert kinds(run_match("", same_day + JD_BUY)) == [("new", "new"), *mismatch]
    # A purchase whose payment method the converter did not know still meets its ledger copy.
    unknown = JD_BUY.replace("Liabilities:CreditCard:BOC:1875", "Assets:Unmapped")
    assert kinds(run_match(JD_BUY, unknown)) == [("duplicate", "order-id")]


def test_refund_in_the_converter_shape_reimported_is_a_duplicate():
    # In the ledger the user has put the refund's Assets:Unmapped side on Expenses:Shopping.
    ledger = JD_BUY + JD_REFUND_CONVERTED.replace("Assets:Unmapped", "Expenses:Shopping")
    staged = JD_BUY.replace("Expenses:Shopping", "Expenses:Unsorted") + JD_REFUND_CONVERTED
    assert kinds(run_match(ledger, staged)) == [("duplicate", "order-id")] * 2
    # The same bill twice in one batch, in date order: buy, buy, refund, refund.
    assert kinds(run_match("", staged + staged)) == [
        ("new", "new"), ("duplicate", "order-id"),
        ("uncertain", "order-id-mismatch"), ("duplicate", "order-id")]


# Final review: rule 2 pairs through a channel only on the channel's account (C1).

LOAN_CHAT = (  # "lent Zhang San 500 via WeChat"
    '2026-09-06 * "张三" "lent Zhang San"\n  via: "chat"\n  channel: "wechat"\n'
    "  Assets:Receivables:Loans:ZhangSan  500.00 CNY\n  Assets:WeChat:Change  -500.00 CNY\n"
)
WX_RECEIPT = (  # an unrelated receipt, its targetAccount resolved
    '2026-09-07 * "李四" "转账"\n  via: "wechat"\n  orderId: "W30"\n'
    "  Assets:WeChat:Change  500.00 CNY\n  Income:Gifts  -500.00 CNY\n"
)
TOPUP_CHAT = (  # "topped up Alipay with 100 from the card"
    '2026-09-06 * "" "top up Alipay"\n  via: "chat"\n  channel: "alipay"\n'
    f"  Assets:Alipay:Balance  100.00 CNY\n  {CARD}  -100.00 CNY\n"
)
ALIPAY_MEAL = (
    '2026-09-06 * "某饭店" "晚餐"\n  via: "alipay"\n  orderId: "A40"\n'
    "  Expenses:Food  100.00 CNY\n  Assets:Alipay:Balance  -100.00 CNY\n"
)
WALLETS = parse_settings(
    {"chat": {"channels": {"wechat": "Assets:WeChat:Change", "alipay": "Assets:Alipay:Balance"}}})


def run_with(settings, existing, staged):
    return match(parse_text(existing), parse_text(staged), settings)


@pytest.mark.parametrize("settings", [SET, WALLETS], ids=["channel-on-card", "channel-on-wallet"])
@pytest.mark.parametrize(("chat", "staged"), [
    (LOAN_CHAT, WX_RECEIPT),
    (LOAN_CHAT.replace("Loans:ZhangSan", "Reimbursement").replace("500.00", "80.00"),
     WX_RECEIPT.replace("500.00", "80.00")),
    (TOPUP_CHAT, ALIPAY_MEAL),
], ids=["loan", "reimbursement", "transfer"])
def test_channel_never_pairs_another_posting(settings, chat, staged):
    # The bill row's amount is on the entry's receivable or transfer side, not on its channel's
    # account: no fix. The entry is near enough to ask about.
    assert kinds(run_with(settings, chat, staged)) == [("uncertain", "possible-chat")]


def test_channel_pairs_on_the_channel_account_only():
    on_cash = CHAT_LUNCH.replace(CARD, "Assets:Cash")  # channel wechat, but not on its account
    assert kinds(run_match(on_cash, WX_LUNCH)) == [("uncertain", "possible-chat")]
    assert kinds(run_with(WALLETS, CHAT_LUNCH, WX_LUNCH)) == [("uncertain", "possible-chat")]


def test_channel_account_falls_back_to_the_chat_default():
    default = parse_settings({"chat": {"default": CARD}})
    [item] = run_with(default, CHAT_LUNCH, WX_LUNCH)
    assert (item.kind, item.fix["account_from"], item.fix["account_to"]) == (
        "fix", CARD, "Assets:WeChat:Change")
    assert kinds(run_with(parse_settings({}), CHAT_LUNCH, WX_LUNCH)) == [
        ("uncertain", "possible-chat")]


def test_fix_never_books_an_account_twice():
    # The channel's account is the card: the card side of the top-up pays the meal, but moving it
    # to Assets:Alipay:Balance would put that account on the entry twice.
    assert kinds(run_match(TOPUP_CHAT, ALIPAY_MEAL)) == [("uncertain", "possible-chat")]
    # The same with a transfer in the bill: a move from 余额 to 余额宝.
    to_yuebao = ALIPAY_MEAL.replace("Expenses:Food", "Assets:Alipay:YuEBao")
    assert kinds(run_match(TOPUP_CHAT, to_yuebao)) == [("uncertain", "possible-chat")]


# Final review: entries added in Fava or by hand count as chat entries (I3).

FAVA_LUNCH = (  # no via: added in Fava
    '2026-09-06 * "食堂" "lunch"\n'
    "  Expenses:Food  35.00 CNY\n  Assets:WeChat:Change  -35.00 CNY\n"
)
OPENING = (
    '2026-09-01 * "Opening balances"\n'
    "  Assets:WeChat:Change  35.00 CNY\n  Assets:Bank:CCB:1234  5230.00 CNY\n"
    "  Equity:Opening-Balances\n"
)


def test_entry_added_in_fava_is_fixed_like_a_chat_entry():
    # It names no channel: the card's own statement fixes it, a platform's row asks (N1).
    on_card = FAVA_LUNCH.replace("Assets:WeChat:Change", CARD)
    pos = BANK_RIDE.replace("2026-09-04", "2026-09-06").replace("20.00", "35.00")
    [item] = run_match(on_card, pos.replace("支付宝-支付宝外部商户-高德打车", "食堂"))
    assert (item.kind, item.reason) == ("fix", "chat")
    assert item.fix["account_from"] == item.fix["account_to"] == CARD
    assert item.fix["meta"] == {"bill": "ccb"}
    assert kinds(run_match(FAVA_LUNCH, WX_LUNCH)) == [("uncertain", "possible-duplicate")]
    with_channel = FAVA_LUNCH.replace('"lunch"\n', '"lunch"\n  channel: "wechat"\n')
    [item] = run_match(with_channel, WX_LUNCH)
    assert (item.kind, item.fix["meta"]["orderId"]) == ("fix", "W1")


def test_entry_added_in_fava_on_another_account_is_asked_about():
    on_cash = FAVA_LUNCH.replace("Assets:WeChat:Change", "Assets:Cash")
    assert kinds(run_match(on_cash, WX_LUNCH)) == [("uncertain", "possible-chat")]


def test_opening_balances_are_never_a_candidate():
    receipt = (
        '2026-09-02 * "某人" "收款"\n  via: "wechat"\n  orderId: "W50"\n'
        "  Assets:WeChat:Change  35.00 CNY\n  Income:Gifts  -35.00 CNY\n"
    )
    assert kinds(run_match(OPENING, receipt)) == [("new", "new")]
    assert kinds(run_match(OPENING, receipt.replace("35.00", "34.00"))) == [("new", "new")]
    salary = (
        '2026-09-02 * "某公司" "工资"\n  via: "ccb"\n'
        "  Assets:Bank:CCB:1234  5230.00 CNY\n  Income:Salary  -5230.00 CNY\n"
    )
    assert kinds(run_match(OPENING, salary)) == [("new", "new")]


# Final review: bill rows dated before the books' start (I1).

STARTED = parse_settings({"ledger": {"start": "2026-09-04"}, "chat": {"channels": {"wechat": CARD}}})


def test_rows_before_the_start_are_skipped():
    items = run_with(STARTED, ALIPAY_RIDE, ALIPAY_RIDE + BANK_RIDE)
    assert kinds(items) == [("duplicate", "before-start"), ("duplicate", "channel")]
    assert items[0].existing == [] and items[0].fix is None
    # A row on the first day is imported; one before it never takes a ledger entry.
    assert kinds(run_with(STARTED, "", BANK_RIDE)) == [("uncertain", "channel-unmatched")]
    earlier = WX_LUNCH.replace("2026-09-06", "2026-09-03").replace('"W1"', '"W0"')
    assert kinds(run_with(STARTED, CHAT_LUNCH, earlier + WX_LUNCH)) == [
        ("duplicate", "before-start"), ("fix", "chat")]


def test_balances_before_the_books_are_left_out(make_ledger, tmp_path):
    d = make_ledger()
    staged = tmp_path / "s.beancount"
    staged.write_text(
        ALIPAY_RIDE + "2026-09-04 balance Assets:Bank:CCB:1234  10.00 CNY\n"
        "2026-09-05 balance Assets:Bank:CCB:1234  20.00 CNY\n", "utf-8")
    summary = run(d / "main.beancount", staged, tmp_path / "plan.json", STARTED)
    assert (summary["duplicate"], summary["new"]) == (1, 0)
    [balance] = json.loads((tmp_path / "plan.json").read_text("utf-8"))["balances"]
    assert balance.startswith("2026-09-05 balance")


# N1: rule 2 fixes a hand entry only when it is surely the same payment.

CHAT_DINNER = (  # "dinner, 200 on the card": no channel
    '2026-10-03 * "饭店" "dinner"\n  via: "chat"\n'
    f"  Expenses:Food  200.00 CNY\n  {CARD}  -200.00 CNY\n"
)
HEADPHONES = (  # an Alipay payment drawn from that card
    '2026-10-04 * "淘宝" "耳机"\n  via: "alipay"\n  orderId: "T1"\n'
    f"  Expenses:Shopping  200.00 CNY\n  {CARD}  -200.00 CNY\n"
)
CCB_DINNER_AND_HEADPHONES = (
    '2026-10-03 * "某饭店" "POS消费"\n  via: "ccb"\n'
    f"  Expenses:Food  200.00 CNY\n  {CARD}  -200.00 CNY\n\n"
    '2026-10-04 * "支付宝" "支付宝-淘宝"\n  via: "ccb"\n'
    f"  Expenses:Shopping  200.00 CNY\n  {CARD}  -200.00 CNY\n"
)


def test_platform_row_never_fixes_an_entry_paid_by_card():  # probe C
    # The user said "card", the bill says Alipay: ask, whether or not the row has a category yet.
    for row in (HEADPHONES, HEADPHONES.replace("Expenses:Shopping", "Expenses:Unsorted")):
        [item] = run_match(CHAT_DINNER, row)
        assert (item.kind, item.reason, item.fix) == ("uncertain", "possible-duplicate", None)
        assert '"dinner"' in item.existing[0]["text"]
    # The headphones are another payment. The card's own statement fixes the dinner with its POS
    # line; its Alipay line is the headphones' echo.
    items = run_match(CHAT_DINNER + "\n" + HEADPHONES, CCB_DINNER_AND_HEADPHONES)
    assert kinds(items) == [("fix", "chat"), ("duplicate", "channel")]
    assert '"dinner"' in items[0].existing[0]["text"]
    assert items[0].fix == {"file": "<string>", "line": 1, "account_from": CARD,
                            "account_to": CARD, "meta": {"bill": "ccb"}}
    assert '"耳机"' in items[1].existing[0]["text"]


def test_platform_row_fixes_only_an_entry_that_names_it():
    on_card = WX_LUNCH.replace("Assets:WeChat:Change", CARD)
    no_channel = CHAT_LUNCH.replace('  channel: "wechat"\n', "")
    other_channel = CHAT_LUNCH.replace('"wechat"', '"alipay"')
    for chat in (no_channel, other_channel, FAVA_LUNCH.replace("Assets:WeChat:Change", CARD)):
        [item] = run_match(chat, on_card)
        assert (item.kind, item.reason, item.fix) == ("uncertain", "possible-duplicate", None)
    # Named: on the channel's account, or on the account the bill shows.
    assert kinds(run_match(CHAT_LUNCH, on_card)) == [("fix", "chat")]
    [item] = run_match(CHAT_LUNCH.replace(CARD, "Assets:WeChat:Change"), WX_LUNCH)
    assert (item.kind, item.fix["account_from"], item.fix["account_to"]) == (
        "fix", "Assets:WeChat:Change", "Assets:WeChat:Change")


LOAN_TRANSFER = (  # the loan's own transfer in the WeChat bill, paid from 零钱
    '2026-09-06 * "张三" "转账"\n  via: "wechat"\n  orderId: "W31"\n'
    "  Expenses:Unsorted  500.00 CNY\n  Assets:WeChat:Change  -500.00 CNY\n"
)


def test_transfer_and_payment_are_never_fixed_silently():
    # Uncategorised, the bill's transfer to 张三 looks like spending: ask.
    [item] = run_with(WALLETS, LOAN_CHAT, LOAN_TRANSFER)
    assert (item.kind, item.reason, item.fix) == ("uncertain", "possible-duplicate", None)
    assert '"lent Zhang San"' in item.existing[0]["text"]
    # Booked to the loan, the row is a transfer too: fixed, the entry's postings kept.
    to_loan = LOAN_TRANSFER.replace("Expenses:Unsorted", "Assets:Receivables:Loans:ZhangSan")
    [item] = run_with(WALLETS, LOAN_CHAT, to_loan)
    assert (item.kind, item.fix["account_to"]) == ("fix", item.fix["account_from"])


@pytest.mark.parametrize("meta", [
    "", '  via: "chat"\n', '  via: "chat"\n  channel: "alipay"\n',
], ids=["fava", "chat", "chat-channel"])
def test_loan_never_absorbs_another_payment(meta):
    loan = (
        f'2026-10-06 * "张三" "lent"\n{meta}'
        f"  Assets:Receivables:Loans:ZhangSan  500.00 CNY\n  {CARD}  -500.00 CNY\n"
    )
    [item] = run_match(loan, HEADPHONES.replace("200.00", "500.00"))
    assert (item.kind, item.reason, item.fix) == ("uncertain", "possible-duplicate", None)
    assert '"lent"' in item.existing[0]["text"]


PAID_BACK = (  # "Zhang San paid back 20 via Alipay"
    '2026-09-06 * "张三" "paid back"\n  via: "chat"\n  channel: "alipay"\n'
    "  Assets:Alipay:Balance  20.00 CNY\n  Assets:Receivables:Loans:ZhangSan  -20.00 CNY\n"
)
UNPLACED_RECEIPT = (  # money in; the converter could not tell what it is
    '2026-09-06 * "张三" "收款"\n  via: "alipay"\n  orderId: "A7"\n'
    "  Assets:Alipay:Balance  20.00 CNY\n  Assets:Unmapped  -20.00 CNY\n"
)


def test_receipt_of_unknown_kind_is_never_fixed_silently():
    [item] = run_with(WALLETS, PAID_BACK, UNPLACED_RECEIPT)
    assert (item.kind, item.reason, item.fix) == ("uncertain", "possible-duplicate", None)
    # Nor does an entry of unknown kind, even against one of the same kind.
    unplaced = PAID_BACK.replace("Assets:Receivables:Loans:ZhangSan", "Assets:Unmapped")
    assert kinds(run_with(WALLETS, unplaced, UNPLACED_RECEIPT)) == [
        ("uncertain", "possible-duplicate")]
    # Placed as the loan paid back, it is a transfer like the entry: fixed.
    placed = UNPLACED_RECEIPT.replace("Assets:Unmapped", "Assets:Receivables:Loans:ZhangSan")
    assert kinds(run_with(WALLETS, PAID_BACK, placed)) == [("fix", "chat")]

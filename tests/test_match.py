import json

import pytest
from ledgerkit.book import format_entry, parse_text
from ledgerkit.cli import main
from ledgerkit.match import match, run
from ledgerkit.settings import LedgerError, parse_settings

SET = parse_settings({})
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
    chat = CHAT_LUNCH.replace('  channel: "wechat"\n', "")
    [item] = run_match(chat, WX_LUNCH.replace("Assets:WeChat:Change", "Assets:Bank:CCB:1234"))
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
    d = make_ledger(txns={2026: ALIPAY_RIDE + CHAT_LUNCH})
    staged = tmp_path / "s.beancount"
    # Every one is unsorted: a duplicate (BANK_RIDE), an uncertain channel line (BANK_TOPUP),
    # a fix (WX_LUNCH, the chat entry keeps its category) and a new one (fund).
    staged.write_text(BANK_RIDE + BANK_TOPUP + WX_LUNCH + fund, "utf-8")
    summary = run(d / "main.beancount", staged, tmp_path / "plan.json", SET)
    assert summary == {
        "plan": str(tmp_path / "plan.json"), "new": 1, "duplicate": 1, "fix": 1, "uncertain": 1,
        "flagged": {"unsorted": 2, "unmapped": 1, "investment": 1},
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

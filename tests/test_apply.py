import io
import json
import shutil
from datetime import date

import pytest
from beancount.core.data import Balance, Document
from ledgerkit import apply as apply_module
from ledgerkit.apply import NeedsDecision, apply_plan
from ledgerkit.book import digest, load, transactions
from ledgerkit.cli import main
from ledgerkit.match import run
from ledgerkit.settings import LedgerError, parse_settings
from test_match import ALIPAY_RIDE, BANK_RIDE, CHAT_LUNCH, SET, WX_LUNCH

OPEN = "2000-01-01 open Assets:Bank:CCB:1234\n2000-01-01 open Assets:WeChat:Change\n"
CARD = "Assets:Bank:CCB:1234"
WX_FOOD = WX_LUNCH.replace("Expenses:Unsorted", "Expenses:Food")  # a bill row the user sorted
# A bank line that may be the echo of ALIPAY_RIDE; it asks the user (possible-duplicate).
SHOP = BANK_RIDE.replace("支付宝-支付宝外部商户-高德打车", "某超市")
SHOP_SORTED = SHOP.replace("Expenses:Unsorted", "Expenses:Other")
CHAT_DINNER = (CHAT_LUNCH.replace("2026-09-06", "2026-09-07").replace("lunch", "dinner")
               .replace("35.00", "48.00"))
WX_DINNER = (WX_FOOD.replace("2026-09-06", "2026-09-07").replace("W1", "W2")
             .replace("35.00", "48.00"))
BALANCE = f"2026-10-01 balance {CARD}  -20.00 CNY\n"


@pytest.fixture
def prepare(make_ledger, tmp_path):
    """prepare(staged, existing="") -> (ledger dir, plan path): the plan `match.run` writes."""

    def prepare(staged, existing=""):
        ledger = make_ledger(OPEN, txns={2026: existing} if existing else None)
        staged_path = tmp_path / "staged.beancount"
        staged_path.write_text(staged, "utf-8", newline="\n")
        plan = tmp_path / "plan.json"
        run(ledger / "main.beancount", staged_path, plan, SET)
        return ledger, plan

    return prepare


def snapshot(ledger):
    return {p.relative_to(ledger): p.read_bytes() for p in ledger.rglob("*") if p.is_file()}


def year_text(ledger):
    return (ledger / "txns" / "2026.beancount").read_text("utf-8")


def read_plan(plan):
    return json.loads(plan.read_text("utf-8"))


def edit_plan(plan, change):
    data = read_plan(plan)
    change(data)
    plan.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8", newline="\n")


def refresh_digest(plan, ledger):
    """Bring the plan's ledger digest up to date, as if the ledger had changed only just before
    the match: a check of each fix, not the digest, is then what sees a change at the fix."""
    edit_plan(plan, lambda p: p.update(ledger_digest=digest(ledger)))


def only_transaction(ledger):
    entries, errors, _ = load(ledger / "main.beancount")
    assert errors == []
    [txn] = transactions(entries)
    return txn


def test_new_items_are_written(prepare):
    d, plan = prepare(ALIPAY_RIDE)
    result = apply_plan(plan, d, {})
    assert result == {"written": 1, "skipped": 0, "fixed": 0, "balances": 0, "document": None}
    assert not (d / "txns" / "2025.beancount").exists()
    text = year_text(d)
    assert text.startswith("; Transactions of 2026") and 'orderId: "A1"' in text
    txn = only_transaction(d)
    assert {p.account for p in txn.postings} == {"Expenses:Transport", CARD}
    # The plan is kept and marked.
    marked = read_plan(plan)
    assert marked["applied"] is True and marked["result"] == result
    assert [item["id"] for item in marked["items"]] == ["s1"]


def test_fix_moves_chat_entry_to_real_account(prepare):
    d, plan = prepare(WX_FOOD, existing=CHAT_LUNCH)
    result = apply_plan(plan, d, {})
    assert result == {"written": 0, "skipped": 0, "fixed": 1, "balances": 0, "document": None}
    txn = only_transaction(d)  # no new transaction was added
    assert {p.account for p in txn.postings} == {"Expenses:Food", "Assets:WeChat:Change"}
    assert txn.meta["orderId"] == "W1" and txn.meta["bill"] == "wechat"
    assert txn.meta["payTime"] == "2026-09-06 12:01:02 +0800 CST"
    assert txn.meta["via"] == "chat" and txn.meta["channel"] == "wechat"
    assert txn.narration == "lunch"
    # The metadata sits right under the header; nothing else moved.
    lines = year_text(d).splitlines()
    assert lines[0] == '2026-09-06 * "" "lunch"'
    assert lines[1:4] == [
        '  orderId: "W1"', '  payTime: "2026-09-06 12:01:02 +0800 CST"', '  bill: "wechat"']
    assert lines[4:] == CHAT_LUNCH.replace(CARD, "Assets:WeChat:Change").splitlines()[1:]


def test_fix_posting_without_amount(prepare):
    chat = CHAT_LUNCH.replace(f"  {CARD}  -35.00 CNY\n", f"  {CARD}\n")
    d, plan = prepare(WX_FOOD, existing=chat)
    assert apply_plan(plan, d, {})["fixed"] == 1
    txn = only_transaction(d)
    assert {p.account for p in txn.postings} == {"Expenses:Food", "Assets:WeChat:Change"}
    assert "\n  Assets:WeChat:Change\n" in year_text(d)


def test_fixes_in_one_file_apply_bottom_up(prepare):
    d, plan = prepare(WX_FOOD + WX_DINNER, existing=CHAT_LUNCH + "\n" + CHAT_DINNER)
    assert [i["kind"] for i in read_plan(plan)["items"]] == ["fix", "fix"]
    assert apply_plan(plan, d, {})["fixed"] == 2
    entries, errors, _ = load(d / "main.beancount")
    assert errors == []
    by_name = {t.narration: t for t in transactions(entries)}
    assert by_name["lunch"].meta["orderId"] == "W1" and by_name["dinner"].meta["orderId"] == "W2"
    for txn in by_name.values():
        assert CARD not in {p.account for p in txn.postings}
        assert txn.meta["bill"] == "wechat"


def test_fix_keeps_present_keys_and_the_indentation(prepare):
    chat = CHAT_LUNCH.replace("  ", "\t").replace(
        '\tchannel: "wechat"\n', '\tchannel: "wechat"\n\tpayTime: "earlier"\n')
    d, plan = prepare(WX_FOOD, existing=chat)
    apply_plan(plan, d, {})
    txn = only_transaction(d)
    assert txn.meta["payTime"] == "earlier" and txn.meta["orderId"] == "W1"
    assert year_text(d).count("payTime") == 1
    assert '\n\torderId: "W1"\n\tbill: "wechat"\n' in year_text(d)


def test_fix_in_a_crlf_file(prepare):
    d, plan = prepare(WX_FOOD, existing=CHAT_LUNCH.replace("\n", "\r\n"))
    apply_plan(plan, d, {})
    data = (d / "txns" / "2026.beancount").read_bytes()
    assert b'\r\n  orderId: "W1"\r\n' in data and data.count(b"\n") == data.count(b"\r\n")
    assert only_transaction(d).meta["orderId"] == "W1"


def test_fix_values_are_escaped(prepare):
    d, plan = prepare(WX_FOOD, existing=CHAT_LUNCH)
    edit_plan(plan, lambda p: p["items"][0]["fix"]["meta"].update(orderId='a"b\\c'))
    apply_plan(plan, d, {})
    assert 'orderId: "a\\"b\\\\c"' in year_text(d)
    assert only_transaction(d).meta["orderId"] == 'a"b\\c'


def test_fix_outside_the_ledger_is_refused(prepare, tmp_path):
    d, plan = prepare(WX_FOOD, existing=CHAT_LUNCH)
    other = tmp_path / "other.beancount"
    other.write_text(CHAT_LUNCH, "utf-8", newline="\n")
    edit_plan(plan, lambda p: p["items"][0]["fix"].update(file=str(other)))
    before = snapshot(d)
    with pytest.raises(LedgerError, match="not in the ledger"):
        apply_plan(plan, d, {})
    assert other.read_text("utf-8") == CHAT_LUNCH and snapshot(d) == before


def test_uncertain_needs_decision(prepare):
    d, plan = prepare(SHOP_SORTED, existing=ALIPAY_RIDE)
    before = snapshot(d)
    with pytest.raises(NeedsDecision) as caught:
        apply_plan(plan, d, {})
    assert caught.value.payload == {"undecided": ["s1"]}
    assert snapshot(d) == before and read_plan(plan)["applied"] is False


@pytest.mark.parametrize("choice, written", [("skip", 0), ("new", 1)])
def test_decisions_new_and_skip(prepare, choice, written):
    d, plan = prepare(SHOP_SORTED, existing=ALIPAY_RIDE)
    result = apply_plan(plan, d, {"s1": choice})
    assert result["written"] == written and result["skipped"] == 1 - written
    entries, errors, _ = load(d / "main.beancount")
    assert errors == [] and len(transactions(entries)) == 1 + written
    assert ("2026-09-04" in year_text(d)) == bool(written)


@pytest.mark.parametrize("decisions, message", [
    ({"s9": "new"}, "no item s9"),
    ({"s2": "maybe"}, "new or skip"),
    ({"s2": "new", "s1": "skip"}, "s1 is new, not uncertain"),
])
def test_bad_decisions_are_errors(prepare, decisions, message):
    d, plan = prepare(SHOP_SORTED + ALIPAY_RIDE.replace("A1", "A7"), existing=ALIPAY_RIDE)
    assert [i["kind"] for i in read_plan(plan)["items"]] == ["new", "uncertain"]
    before = snapshot(d)
    with pytest.raises(LedgerError, match=message):
        apply_plan(plan, d, decisions)
    assert snapshot(d) == before and read_plan(plan)["applied"] is False


def test_unsorted_is_refused(prepare):
    d, plan = prepare(ALIPAY_RIDE.replace("Expenses:Transport", "Expenses:Unsorted"))
    before = snapshot(d)
    with pytest.raises(NeedsDecision) as caught:
        apply_plan(plan, d, {})
    assert caught.value.payload == {"unresolved": [{"id": "s1", "flags": ["unsorted"]}]}
    assert snapshot(d) == before and read_plan(plan)["applied"] is False


def test_unsorted_item_decided_new_is_refused(prepare):
    d, plan = prepare(SHOP, existing=ALIPAY_RIDE)  # Expenses:Unsorted
    with pytest.raises(NeedsDecision) as caught:
        apply_plan(plan, d, {"s1": "new"})
    assert caught.value.payload == {"unresolved": [{"id": "s1", "flags": ["unsorted"]}]}
    # Skipping the item writes nothing, so nothing is unresolved.
    assert apply_plan(plan, d, {"s1": "skip"})["skipped"] == 1


def test_flagged_fixes_and_duplicates_do_not_block(prepare):
    # WX_LUNCH keeps its Expenses:Unsorted: the fix leaves the chat entry's Expenses:Food alone.
    unsorted_ride = ALIPAY_RIDE.replace("Expenses:Transport", "Expenses:Unsorted")
    d, plan = prepare(WX_LUNCH + unsorted_ride, existing=CHAT_LUNCH + "\n" + ALIPAY_RIDE)
    items = read_plan(plan)["items"]
    assert [(i["kind"], i["flags"]) for i in items] == [
        ("duplicate", ["unsorted"]), ("fix", ["unsorted"])]
    result = apply_plan(plan, d, {})
    assert result["fixed"] == 1 and result["written"] == 0 and result["skipped"] == 1
    entries, errors, _ = load(d / "main.beancount")
    assert errors == [] and len(transactions(entries)) == 2


def test_balances_are_written(prepare):
    d, plan = prepare(ALIPAY_RIDE + BALANCE)
    result = apply_plan(plan, d, {})
    assert result["written"] == 1 and result["balances"] == 1
    assert f"2026-10-01 balance {CARD}" in " ".join(year_text(d).split())
    entries, errors, _ = load(d / "main.beancount")
    assert errors == [] and len([e for e in entries if isinstance(e, Balance)]) == 1


def test_bad_balance_rolls_back(prepare):
    staged = WX_FOOD + ALIPAY_RIDE + BALANCE.replace("-20.00", "2140.13")
    d, plan = prepare(staged, existing=CHAT_LUNCH)
    before = snapshot(d)
    with pytest.raises(LedgerError, match="ledger check failed"):
        apply_plan(plan, d, {})
    # The fix and the new entry are both undone.
    assert snapshot(d) == before and read_plan(plan)["applied"] is False


def test_plan_applied_twice_is_refused(prepare):
    d, plan = prepare(ALIPAY_RIDE + BALANCE)
    apply_plan(plan, d, {})
    after_first = snapshot(d)
    with pytest.raises(LedgerError, match="plan already applied"):
        apply_plan(plan, d, {})
    assert snapshot(d) == after_first
    assert len(transactions(load(d / "main.beancount")[0])) == 1


def test_applied_check_comes_before_the_decisions(prepare):
    d, plan = prepare(SHOP_SORTED, existing=ALIPAY_RIDE)
    apply_plan(plan, d, {"s1": "skip"})
    with pytest.raises(LedgerError, match="plan already applied"):
        apply_plan(plan, d, {})


def test_same_bill_matched_again_adds_nothing(prepare, tmp_path):
    d, plan = prepare(ALIPAY_RIDE + BALANCE)
    apply_plan(plan, d, {})
    after_first = snapshot(d)
    again = tmp_path / "again.json"
    run(d / "main.beancount", tmp_path / "staged.beancount", again, SET)
    assert [i["kind"] for i in read_plan(again)["items"]] == ["duplicate"]
    result = apply_plan(again, d, {})
    assert result == {"written": 0, "skipped": 1, "fixed": 0, "balances": 0, "document": None}
    assert snapshot(d) == after_first  # not even the balance line is written twice


@pytest.mark.parametrize("refresh", [False, True], ids=["digest", "fix-check"])
def test_stale_plan_is_refused(prepare, refresh):
    d, plan = prepare(WX_FOOD, existing=CHAT_LUNCH)
    path = d / "txns" / "2026.beancount"
    path.write_text("; a note above\n" + CHAT_LUNCH, "utf-8", newline="\n")
    if refresh:
        refresh_digest(plan, d)
    before = snapshot(d)
    with pytest.raises(LedgerError, match="run match again"):
        apply_plan(plan, d, {})
    assert snapshot(d) == before and read_plan(plan)["applied"] is False


def test_stale_plan_with_another_entry_on_the_same_day(prepare):
    d, plan = prepare(WX_FOOD, existing=CHAT_LUNCH)
    other = CHAT_LUNCH.replace("lunch", "coffee").replace("35.00", "9.00")
    path = d / "txns" / "2026.beancount"
    path.write_text(other + "\n" + CHAT_LUNCH, "utf-8", newline="\n")  # line 1 is now `coffee`
    refresh_digest(plan, d)
    before = snapshot(d)
    with pytest.raises(LedgerError, match="run match again"):
        apply_plan(plan, d, {})
    assert snapshot(d) == before


@pytest.mark.parametrize("stand_in", [
    CHAT_LUNCH.replace("35.00", "12.00"),
    CHAT_LUNCH.replace("Expenses:Food", "Expenses:Other"),
    CHAT_LUNCH.replace("  Expenses:Food  35.00 CNY\n",
                       "  Expenses:Food  25.00 CNY\n  Expenses:Other  10.00 CNY\n"),
    CHAT_LUNCH.replace("  Expenses:Food  35.00 CNY\n", "  Expenses:Food  35.00 USD\n"),
], ids=["other-amount", "other-account", "extra-posting", "other-currency"])
def test_stale_plan_with_a_look_alike_entry_at_the_line(prepare, stand_in):
    # The same day, the same narration and the same funding account: only the postings differ.
    # This is what a second plan finds once the first apply has inserted lines above its entry.
    d, plan = prepare(WX_FOOD, existing=CHAT_LUNCH)
    path = d / "txns" / "2026.beancount"
    path.write_text(stand_in + "\n" + CHAT_LUNCH, "utf-8", newline="\n")
    refresh_digest(plan, d)
    before = snapshot(d)
    with pytest.raises(LedgerError, match="run match again"):
        apply_plan(plan, d, {})
    assert snapshot(d) == before and read_plan(plan)["applied"] is False


@pytest.mark.parametrize("food", [
    "  Expenses:Food\n",
    "  Expenses:Food  35.0 CNY\n",
    "  Expenses:Food  (30 + 5) CNY\n",
], ids=["elided", "other-spelling", "expression"])
def test_fix_accepts_the_same_postings_written_another_way(prepare, food):
    chat = CHAT_LUNCH.replace("  Expenses:Food  35.00 CNY\n", food)
    d, plan = prepare(WX_FOOD, existing=chat)
    assert apply_plan(plan, d, {})["fixed"] == 1
    txn = only_transaction(d)
    assert {p.account for p in txn.postings} == {"Expenses:Food", "Assets:WeChat:Change"}
    assert txn.meta["orderId"] == "W1"


def test_stale_plan_checks_every_fix_before_writing(prepare):
    d, plan = prepare(WX_FOOD + WX_DINNER, existing=CHAT_LUNCH + "\n" + CHAT_DINNER)
    path = d / "txns" / "2026.beancount"
    # The dinner entry is where the plan says (and is fixed first, bottom up); lunch changed.
    path.write_text(
        CHAT_LUNCH.replace("lunch", "brunch") + "\n" + CHAT_DINNER, "utf-8", newline="\n")
    refresh_digest(plan, d)
    before = snapshot(d)
    with pytest.raises(LedgerError, match="run match again"):
        apply_plan(plan, d, {})
    assert snapshot(d) == before


def test_document_with_chinese_name(prepare, tmp_path):
    d, plan = prepare(ALIPAY_RIDE + WX_FOOD)  # the latest entry is dated 2026-09-06
    folder = tmp_path / "下载 files"
    folder.mkdir()
    bill = folder / "交易明细 2026-09.xls"
    bill.write_bytes(b"\xd0\xd0\x00 original bytes \xff")
    result = apply_plan(plan, d, {}, document=bill, document_account=CARD)
    name = "documents/Assets/Bank/CCB/1234/2026-09-06.交易明细 2026-09.xls"
    assert result["document"] == name and result["written"] == 2
    assert (d / name).read_bytes() == bill.read_bytes() and bill.exists()
    assert read_plan(plan)["result"]["document"] == name
    entries, errors, _ = load(d / "main.beancount")
    assert errors == []
    [doc] = [e for e in entries if isinstance(e, Document)]
    assert doc.account == CARD and doc.filename.endswith("2026-09-06.交易明细 2026-09.xls")


def test_ledger_in_a_folder_with_spaces_and_chinese(make_ledger, tmp_path):
    ledger = tmp_path / "我的 账本 2026"
    shutil.copytree(make_ledger(OPEN, txns={2026: CHAT_LUNCH}), ledger)
    staged = tmp_path / "待导入 bill.beancount"
    staged.write_text(WX_FOOD + ALIPAY_RIDE, "utf-8", newline="\n")
    plan = tmp_path / "计划 plan.json"
    run(ledger / "main.beancount", staged, plan, SET)
    bill = tmp_path / "交易明细 2026-09.xls"
    bill.write_bytes(b"x")
    result = apply_plan(plan, ledger, {}, document=bill, document_account=CARD)
    assert (result["fixed"], result["written"]) == (1, 1)
    assert (ledger / result["document"]).read_bytes() == b"x"
    entries, errors, _ = load(ledger / "main.beancount")
    assert errors == [] and len(transactions(entries)) == 2


def test_document_is_dated_today_when_the_plan_has_no_items(prepare, tmp_path):
    d, plan = prepare(BALANCE.replace("-20.00", "0.00"))
    bill = tmp_path / "statement.pdf"
    bill.write_bytes(b"%PDF")
    result = apply_plan(plan, d, {}, document=bill, document_account=CARD)
    assert result["document"] == f"documents/Assets/Bank/CCB/1234/{date.today()}.statement.pdf"
    assert (d / result["document"]).read_bytes() == b"%PDF"


def test_document_account_must_be_open(prepare, tmp_path):
    d, plan = prepare(ALIPAY_RIDE)
    bill = tmp_path / "bill.xls"
    bill.write_bytes(b"x")
    before = snapshot(d)
    with pytest.raises(LedgerError, match="not open"):
        apply_plan(plan, d, {}, document=bill, document_account="Assets:Bank:Nope")
    assert snapshot(d) == before and read_plan(plan)["applied"] is False


def test_document_account_must_be_open_on_the_document_date(prepare, tmp_path):
    d, plan = prepare(ALIPAY_RIDE)
    (d / "accounts.beancount").write_text(
        (d / "accounts.beancount").read_text("utf-8") + "2026-12-01 open Assets:Later\n",
        "utf-8", newline="\n")
    refresh_digest(plan, d)
    bill = tmp_path / "bill.xls"
    bill.write_bytes(b"x")
    with pytest.raises(LedgerError, match="not open on 2026-09-03"):
        apply_plan(plan, d, {}, document=bill, document_account="Assets:Later")


def test_document_target_is_never_overwritten(prepare, tmp_path):
    d, plan = prepare(ALIPAY_RIDE)
    taken = d / "documents/Assets/Bank/CCB/1234/2026-09-03.bill.xls"
    taken.parent.mkdir(parents=True)
    taken.write_bytes(b"older")
    bill = tmp_path / "bill.xls"
    bill.write_bytes(b"newer")
    before = snapshot(d)
    with pytest.raises(LedgerError, match="already exists"):
        apply_plan(plan, d, {}, document=bill, document_account=CARD)
    assert snapshot(d) == before and taken.read_bytes() == b"older"
    assert read_plan(plan)["applied"] is False


def test_document_and_account_go_together(prepare, tmp_path):
    d, plan = prepare(ALIPAY_RIDE)
    bill = tmp_path / "bill.xls"
    bill.write_bytes(b"x")
    before = snapshot(d)
    with pytest.raises(LedgerError, match="go together"):
        apply_plan(plan, d, {}, document=bill)
    with pytest.raises(LedgerError, match="go together"):
        apply_plan(plan, d, {}, document_account=CARD)
    with pytest.raises(LedgerError, match="no such file"):
        apply_plan(plan, d, {}, document=tmp_path / "gone.xls", document_account=CARD)
    assert snapshot(d) == before


def test_document_copy_failure_still_marks_the_plan_applied(prepare, tmp_path, monkeypatch):
    d, plan = prepare(ALIPAY_RIDE)
    bill = tmp_path / "bill.xls"
    bill.write_bytes(b"x")

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(apply_module, "_copy_document", fail)
    with pytest.raises(LedgerError, match="could not be copied"):
        apply_plan(plan, d, {}, document=bill, document_account=CARD)
    # The entries are in the ledger, so the plan must not be applied again.
    assert read_plan(plan)["applied"] is True and read_plan(plan)["result"]["document"] is None
    assert len(transactions(load(d / "main.beancount")[0])) == 1
    assert not list((d / "documents").rglob("*.xls"))
    with pytest.raises(LedgerError, match="plan already applied"):
        apply_plan(plan, d, {})


def test_not_a_plan(tmp_path, make_ledger):
    d = make_ledger(OPEN)
    junk = tmp_path / "junk.json"
    junk.write_text("[1, 2]", "utf-8")
    with pytest.raises(LedgerError, match="not a plan"):
        apply_plan(junk, d, {})
    with pytest.raises(LedgerError, match="cannot read the plan"):
        apply_plan(tmp_path / "missing.json", d, {})


def test_cli_exit_code_2(prepare, capsys):
    d, plan = prepare(SHOP_SORTED, existing=ALIPAY_RIDE)
    assert main(["apply", "--ledger-dir", str(d), "--plan", str(plan)]) == 2
    out = capsys.readouterr()
    assert json.loads(out.out) == {"undecided": ["s1"]} and out.err == ""


def test_cli_unresolved_exits_2(prepare, capsys):
    d, plan = prepare(ALIPAY_RIDE.replace("Expenses:Transport", "Expenses:Unsorted"))
    assert main(["apply", "--ledger-dir", str(d), "--plan", str(plan)]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "unresolved": [{"id": "s1", "flags": ["unsorted"]}]}


def test_cli_apply_with_decision_and_document(prepare, tmp_path, capsys):
    d, plan = prepare(SHOP_SORTED, existing=ALIPAY_RIDE)
    bill = tmp_path / "交易明细 2026-09.xls"
    bill.write_bytes(b"x")
    code = main(["apply", "--ledger-dir", str(d), "--plan", str(plan), "--decide", "s1=new",
                 "--document", str(bill), "--document-account", CARD])
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {
        "written": 1, "skipped": 0, "fixed": 0, "balances": 0,
        "document": "documents/Assets/Bank/CCB/1234/2026-09-04.交易明细 2026-09.xls"}


def test_cli_errors_exit_1(prepare, capsys):
    d, plan = prepare(SHOP_SORTED, existing=ALIPAY_RIDE)
    assert main(["apply", "--ledger-dir", str(d), "--plan", str(plan), "--decide", "s1"]) == 1
    assert "lk: " in capsys.readouterr().err
    args = ["apply", "--ledger-dir", str(d), "--plan", str(plan)]
    assert main([*args, "--decide", "s1=new", "--decide", "s1=skip"]) == 1
    assert "two different decisions" in capsys.readouterr().err
    assert main([*args, "--decide", "s1=skip"]) == 0
    capsys.readouterr()
    assert main(args) == 1
    assert "plan already applied" in capsys.readouterr().err


def test_fix_that_books_an_account_twice_is_refused(prepare):
    d, plan = prepare(WX_FOOD, existing=CHAT_LUNCH)
    # Expenses:Food is the entry's other posting.
    edit_plan(plan, lambda p: p["items"][0]["fix"].update(account_to="Expenses:Food"))
    before = snapshot(d)
    with pytest.raises(LedgerError, match="s1: .*Expenses:Food twice"):
        apply_plan(plan, d, {})
    assert snapshot(d) == before and read_plan(plan)["applied"] is False


def test_fix_of_an_entry_added_in_fava(make_ledger, tmp_path):
    d = make_ledger(OPEN)
    web = d / "txns" / "web.beancount"
    # Paid by card, no channel: the card's own statement line fixes it.
    fava = WX_FOOD.replace('  via: "wechat"\n  orderId: "W1"\n', "").replace(
        '  payTime: "2026-09-06 12:01:02 +0800 CST"\n', "").replace("Assets:WeChat:Change", CARD)
    web.write_text(web.read_text("utf-8") + "\n" + fava, "utf-8", newline="\n")
    staged = tmp_path / "staged.beancount"
    staged.write_text(SHOP.replace("2026-09-04", "2026-09-06").replace("20.00", "35.00"), "utf-8",
                      newline="\n")
    plan = tmp_path / "plan.json"
    assert run(d / "main.beancount", staged, plan, SET)["fix"] == 1
    assert apply_plan(plan, d, {})["fixed"] == 1
    txn = only_transaction(d)
    assert txn.meta["bill"] == "ccb" and "orderId" not in txn.meta and "via" not in txn.meta
    assert {p.account for p in txn.postings} == {"Expenses:Food", CARD}
    assert txn.meta["filename"].endswith("web.beancount")


def test_rows_before_the_start_are_not_written(make_ledger, tmp_path):
    # The accounts open on the first day of the books, as setup writes them.
    d = make_ledger(OPEN.replace("2000-01-01", "2026-09-04"))
    staged = tmp_path / "staged.beancount"
    staged.write_text(ALIPAY_RIDE + SHOP_SORTED.replace("2026-09-04", "2026-09-05")
                      + "2026-09-04 balance Assets:Bank:CCB:1234  0.00 CNY\n", "utf-8", newline="\n")
    plan = tmp_path / "plan.json"
    run(d / "main.beancount", staged, plan, parse_settings({"ledger": {"start": "2026-09-04"}}))
    result = apply_plan(plan, d, {})
    assert (result["written"], result["skipped"], result["balances"]) == (1, 1, 0)
    entries, errors, _ = load(d / "main.beancount")
    assert errors == [] and [t.date.isoformat() for t in transactions(entries)] == ["2026-09-05"]


# Final review: a plan is stale once anything in the ledger changed (I2).


def test_entry_added_between_match_and_apply_is_refused(prepare, monkeypatch, capsys):
    d, plan = prepare(ALIPAY_RIDE)
    # The user tells about the same ride in chat before the plan is applied.
    told = ALIPAY_RIDE.replace('  via: "alipay"\n  orderId: "A1"\n', "")
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(told.encode("utf-8"))))
    assert main(["add", "--ledger-dir", str(d)]) == 0
    capsys.readouterr()
    before = snapshot(d)
    with pytest.raises(LedgerError, match="ledger changed since match; run match again"):
        apply_plan(plan, d, {})
    assert snapshot(d) == before and read_plan(plan)["applied"] is False
    assert len(transactions(load(d / "main.beancount")[0])) == 1


def test_any_ledger_file_counts(prepare):
    d, plan = prepare(ALIPAY_RIDE)
    prices = d / "prices.beancount"
    prices.write_text(prices.read_text("utf-8") + "\n; a comment\n", "utf-8", newline="\n")
    with pytest.raises(LedgerError, match="run match again"):
        apply_plan(plan, d, {})


def test_retry_after_a_crash_before_the_plan_is_marked(prepare, monkeypatch):
    d, plan = prepare(ALIPAY_RIDE)

    def crash(*_args):
        raise OSError("power cut")

    monkeypatch.setattr(apply_module, "_save_plan", crash)
    with pytest.raises(LedgerError, match="do not apply it again"):
        apply_plan(plan, d, {})
    monkeypatch.undo()
    after_first = snapshot(d)
    with pytest.raises(LedgerError, match="run match again"):
        apply_plan(plan, d, {})
    assert snapshot(d) == after_first
    assert len(transactions(load(d / "main.beancount")[0])) == 1


def test_plan_without_a_digest_is_refused(prepare):
    d, plan = prepare(ALIPAY_RIDE)
    edit_plan(plan, lambda p: p.pop("ledger_digest"))
    with pytest.raises(LedgerError, match="run match again"):
        apply_plan(plan, d, {})
    assert not (d / "txns" / "2026.beancount").exists()


def test_plan_for_another_ledger_is_refused(prepare, tmp_path):
    d, plan = prepare(ALIPAY_RIDE)
    other = tmp_path / "other"
    shutil.copytree(d, other)  # the same files, so the same digest
    before = snapshot(other)
    with pytest.raises(LedgerError, match="another ledger"):
        apply_plan(plan, other, {})
    assert snapshot(other) == before and read_plan(plan)["applied"] is False
    assert apply_plan(plan, d, {})["written"] == 1

import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from ledgerkit.book import PLACEHOLDERS, parse_text, transactions
from ledgerkit.cli import main
from ledgerkit.convert import NEVER, build_config, closing_balance, convert, normalize
from ledgerkit.settings import SOURCES, LedgerError, parse_settings

ROOT = Path(__file__).parents[1]
DEFAULTS = ROOT / "ledger" / "importers"
FIXTURES = ROOT / "tests" / "fixtures" / "deb"

BANK_ACCOUNT = "Assets:Bank:CCB:1234"
BANK_TXN = (
    '2026-09-30 * "p" "n"\n  balances: "{balance}"\n'
    f"  Expenses:Food 20.00 CNY\n  {BANK_ACCOUNT} -20.00 CNY\n"
)


def bank_output(*balances: str) -> str:
    """What the converter writes for a bank statement: one dated row per balance."""
    rows = [
        BANK_TXN.format(balance=b).replace("2026-09-30", f"2026-09-{day:02d}")
        for day, b in enumerate(balances, start=28)
    ]
    return '1970-01-01 open Assets:Bank:CCB:1234\n\n' + "\n".join(rows)



def test_rules_defaults_then_user(tmp_path):
    d = tmp_path / "ledger"
    (d / "importers").mkdir(parents=True)
    (d / "importers/alipay.yaml").write_text(
        'rules:\n  - method: "(1234)"\n    methodAccount: Assets:Bank:CCB:1234\n', "utf-8")
    cfg = build_config(SOURCES["alipay"], parse_settings({}), DEFAULTS, d, None)
    rules = cfg["alipay"]["rules"]
    assert rules[0]["methodAccount"] == "Assets:Alipay:Balance"  # app default first
    assert rules[-1]["methodAccount"] == "Assets:Bank:CCB:1234"  # user rule last, wins
    assert cfg["defaultPlusAccount"] == "Expenses:Unsorted"


def test_config_globals(tmp_path):
    settings = parse_settings({"ledger": {"currency": "USD"}})
    cfg = build_config(SOURCES["wechat"], settings, DEFAULTS, tmp_path, None)  # no user file
    assert cfg["title"] == "ledger" and cfg["defaultCurrency"] == "USD"
    assert cfg["defaultMinusAccount"] == "Assets:Unmapped"
    assert cfg["defaultPlusAccount"] == "Expenses:Unsorted"
    assert "defaultCashAccount" not in cfg  # platforms have no card account
    assert [r["methodAccount"] for r in cfg["wechat"]["rules"]] == [
        "Assets:WeChat:Change", "Assets:WeChat:LingQianTong"]


def test_user_rules_file_must_hold_a_rules_list(tmp_path):
    (tmp_path / "importers").mkdir()
    path = tmp_path / "importers/alipay.yaml"
    for text in ("rules: nope\n", "- a\n- b\n", "rules:\n  - nope\n", "rules: [\n"):
        path.write_text(text, "utf-8")
        with pytest.raises(LedgerError, match="alipay.yaml"):
            build_config(SOURCES["alipay"], parse_settings({}), DEFAULTS, tmp_path, None)


def test_user_rules_file_may_be_empty(tmp_path):
    (tmp_path / "importers").mkdir()
    for text in ("", "# nothing yet\nrules:\n"):
        (tmp_path / "importers/alipay.yaml").write_text(text, "utf-8")
        cfg = build_config(SOURCES["alipay"], parse_settings({}), DEFAULTS, tmp_path, None)
        assert cfg["alipay"]["rules"] == build_config(
            SOURCES["alipay"], parse_settings({}), DEFAULTS, tmp_path / "none", None
        )["alipay"]["rules"]


def test_bank_needs_account_and_never_rule(tmp_path):
    with pytest.raises(LedgerError, match="--account is required for bank statements"):
        build_config(SOURCES["ccb"], parse_settings({}), DEFAULTS, tmp_path, None)
    cfg = build_config(SOURCES["ccb"], parse_settings({}), DEFAULTS, tmp_path, "Assets:Bank:CCB:1234")
    assert cfg["defaultCashAccount"] == "Assets:Bank:CCB:1234"
    assert cfg["ccb"]["rules"][-1]["peer"] == NEVER
    assert cfg["ccb"]["rules"][-1]["targetAccount"] == "Expenses:Unsorted"


def test_bank_never_rule_follows_user_rules(tmp_path):
    (tmp_path / "importers").mkdir()
    (tmp_path / "importers/ccb.yaml").write_text(
        "rules:\n  - peer: Canteen\n    targetAccount: Expenses:Food\n", "utf-8")
    rules = build_config(
        SOURCES["ccb"], parse_settings({}), DEFAULTS, tmp_path, "Assets:Bank:CCB:1234"
    )["ccb"]["rules"]
    assert [r["peer"] for r in rules] == ["Canteen", NEVER]


def test_agent_read_source_has_no_converter(tmp_path):
    with pytest.raises(LedgerError, match="lk table"):
        build_config(SOURCES["nbcb"], parse_settings({}), DEFAULTS, tmp_path, "Assets:Bank:NBCB:1")


def test_default_rule_files():
    alipay = yaml.safe_load((DEFAULTS / "alipay.yaml").read_text("utf-8"))["rules"]
    assert {r["method"]: r["methodAccount"] for r in alipay if "method" in r} == {
        "余额": "Assets:Alipay:Balance",
        "余额宝": "Assets:Alipay:YuEBao",
        "花呗": "Liabilities:Alipay:Huabei",
    }
    assert [r["fullMatch"] for r in alipay if r.get("method") in ("余额", "余额宝")] == [True, True]
    assert {r["category"]: r["targetAccount"] for r in alipay if "category" in r} == {
        "餐饮美食": "Expenses:Food",
        "交通出行": "Expenses:Transport",
        "日用百货": "Expenses:Groceries",
        "充值缴费": "Expenses:Utilities",
        "医疗健康": "Expenses:Health",
    }
    wechat = yaml.safe_load((DEFAULTS / "wechat.yaml").read_text("utf-8"))["rules"]
    assert wechat == [
        {"method": "零钱", "fullMatch": True, "methodAccount": "Assets:WeChat:Change"},
        {"method": "零钱通", "fullMatch": True, "methodAccount": "Assets:WeChat:LingQianTong"},
    ]
    for name in ("alipay", "wechat"):
        assert "ledger/importers/" in (DEFAULTS / f"{name}.yaml").read_text("utf-8").split("\n")[0]


def test_normalize_placeholders():
    e = parse_text(
        '2026-09-03 * "x" "y"\n  Expenses:FIXME  20.00 CNY\n  Assets:FIXME  -20.00 CNY\n'
        '2026-09-04 * "x" "y"\n  Assets:Alipay:Balance  5.00 CNY\n  Expenses:Unsorted  -5.00 CNY\n')
    a, b = normalize(e, "alipay")
    assert [p.account for p in a.postings] == ["Expenses:Unsorted", "Assets:Unmapped"]
    assert [p.account for p in b.postings] == ["Assets:Alipay:Balance", "Income:Unsorted"]
    assert a.meta["via"] == "alipay"


def test_normalize_by_sign_and_root():
    e = parse_text(
        '2026-09-03 * "x" "y"\n  Income:FIXME  -20.00 CNY\n  Liabilities:FIXME  20.00 CNY\n'
        '2026-09-04 * "x" "y"\n  Assets:Unmapped  5.00 CNY\n  Expenses:Unsorted  -5.00 CNY\n'
        '2026-09-05 * "x" "y"\n  Expenses:Food  5.00 CNY\n  Assets:Cash  -5.00 CNY\n')
    a, b, c = normalize(e, "wechat")
    assert [p.account for p in a.postings] == ["Income:Unsorted", "Assets:Unmapped"]
    assert [p.account for p in b.postings] == ["Assets:Unmapped", "Income:Unsorted"]
    assert [p.account for p in c.postings] == ["Expenses:Food", "Assets:Cash"]  # real accounts stay
    assert all(t.meta["via"] == "wechat" for t in (a, b, c))
    assert [p.units.number for p in a.postings] == [Decimal("-20.00"), Decimal("20.00")]


def test_normalize_is_idempotent_and_leaves_other_entries():
    e = parse_text(
        '2026-09-03 * "x" "y"\n  Expenses:FIXME  20.00 CNY\n  Assets:FIXME  -20.00 CNY\n'
        "2026-09-04 open Assets:Cash\n")
    once = normalize(e, "jd")
    assert normalize(once, "jd") == once
    assert once[1] is e[1]  # not a transaction: untouched
    assert {p.account for t in once[:1] for p in t.postings} <= PLACEHOLDERS


def test_closing_balance_only_when_unambiguous():
    one = parse_text(
        '2026-09-30 * "p" "n"\n  balances: "2140.13"\n  Expenses:Food 20.00 CNY\n'
        "  Assets:Bank:CCB:1234 -20.00 CNY\n")
    b = closing_balance(one, "Assets:Bank:CCB:1234", "CNY")
    assert (str(b.date), str(b.amount)) == ("2026-10-01", "2140.13 CNY")
    assert b.account == "Assets:Bank:CCB:1234"
    two = one + parse_text(
        '2026-09-30 * "p" "n"\n  balances: "2100.00"\n  Expenses:Food 40.13 CNY\n'
        "  Assets:Bank:CCB:1234 -40.13 CNY\n")
    assert closing_balance(two, "Assets:Bank:CCB:1234", "CNY") is None
    assert closing_balance(one, "Liabilities:CreditCard:ICBC:5678", "CNY") is None


def test_closing_balance_uses_the_latest_day_and_a_year_boundary():
    rows = parse_text(
        '2025-12-30 * "p" "n"\n  balances: "100.00"\n  Expenses:Food 1.00 CNY\n  Assets:Cash -1.00 CNY\n'
        '2025-12-31 * "p" "n"\n  balances: "99.00"\n  Expenses:Food 1.00 CNY\n  Assets:Cash -1.00 CNY\n')
    b = closing_balance(rows, "Assets:Cash", "CNY")
    assert (str(b.date), str(b.amount)) == ("2026-01-01", "99.00 CNY")


@pytest.mark.parametrize("value", ["1.2E+06", "1E+6", "1,200.00", "abc", "", "  ", "NaN", "-"])
def test_closing_balance_refuses_values_that_are_not_plain_decimals(value):
    rows = parse_text(BANK_TXN.format(balance=value))
    assert closing_balance(rows, BANK_ACCOUNT, "CNY") is None


@pytest.mark.parametrize("value, shown", [("0", "0"), ("-15.50", "-15.50"), ("1999.99", "1999.99")])
def test_closing_balance_accepts_plain_decimals(value, shown):
    b = closing_balance(parse_text(BANK_TXN.format(balance=value)), BANK_ACCOUNT, "CNY")
    assert str(b.amount) == f"{shown} CNY"


def test_closing_balance_without_balances_meta():
    rows = parse_text('2026-09-30 * "p" "n"\n  Expenses:Food 20.00 CNY\n  Assets:Cash -20.00 CNY\n')
    assert closing_balance(rows, "Assets:Cash", "CNY") is None
    assert closing_balance([], "Assets:Cash", "CNY") is None


def test_convert_passes_original_file(tmp_path, fake_deb):
    deb, log = fake_deb
    bill = FIXTURES / "alipay/example-alipay-records.csv"
    convert(SOURCES["alipay"], bill, tmp_path / "s.beancount", settings=parse_settings({}),
            defaults_dir=DEFAULTS, ledger_dir=tmp_path, account=None, deb=str(deb))
    rec = json.loads(log.read_text("utf-8"))
    assert rec["argv"][rec["argv"].index("--config") + 1].endswith(".yaml")
    assert rec["sha256"] == hashlib.sha256(bill.read_bytes()).hexdigest()


def test_convert_command_line(tmp_path, fake_deb):
    deb, log = fake_deb
    bill = FIXTURES / "wechat/example-wechat-records.csv"
    convert(SOURCES["wechat"], bill, tmp_path / "s.beancount", settings=parse_settings({}),
            defaults_dir=DEFAULTS, ledger_dir=tmp_path, account=None, deb=str(deb))
    argv = json.loads(log.read_text("utf-8"))["argv"]
    assert argv[:5] == ["translate", "-p", "wechat", "-t", "beancount"]
    assert "-c" not in argv  # --config has no short form
    assert argv[-2:] == ["--ignore-invalid-tx-types", str(bill)]  # extra args, then the bill
    config = yaml.safe_load(json.loads(log.read_text("utf-8"))["config"])
    assert config["wechat"]["rules"][0]["methodAccount"] == "Assets:WeChat:Change"


def test_convert_paths_with_spaces_and_chinese(tmp_path, fake_deb):
    deb, _ = fake_deb
    folder = tmp_path / "my bills"
    folder.mkdir()
    bill = folder / "交易明细 2026-09.csv"
    bill.write_bytes(b"x")
    out = convert(SOURCES["alipay"], bill, folder / "暂存 s.beancount", settings=parse_settings({}),
                  defaults_dir=DEFAULTS, ledger_dir=tmp_path, account=None, deb=str(deb))
    assert out["entries"] == 1 and Path(out["staged"]).exists()
    assert Path(out["staged"]).name == "暂存 s.beancount"


def test_convert_writes_normalized_entries(tmp_path, fake_deb):
    deb, _ = fake_deb
    out = convert(SOURCES["alipay"], FIXTURES / "alipay/example-alipay-records.csv",
                  tmp_path / "new" / "s.beancount", settings=parse_settings({}),
                  defaults_dir=DEFAULTS, ledger_dir=tmp_path, account=None, deb=str(deb))
    assert out == {"staged": str(tmp_path / "new" / "s.beancount"), "entries": 1, "balance": None}
    text = (tmp_path / "new" / "s.beancount").read_text("utf-8")
    assert "FIXME" not in text and "option" not in text and "open" not in text
    (txn,) = parse_text(text)
    assert txn.meta["via"] == "alipay" and txn.meta["orderId"] == "O1"
    assert [p.account for p in txn.postings] == ["Expenses:Unsorted", "Assets:Alipay:Balance"]


def test_convert_bank_stages_a_closing_balance(tmp_path, make_fake_deb):
    deb, _ = make_fake_deb(bank_output("2160.13", "2140.13"))
    out = convert(SOURCES["ccb"], FIXTURES / "alipay/example-alipay-records.csv",
                  tmp_path / "s.beancount", settings=parse_settings({}), defaults_dir=DEFAULTS,
                  ledger_dir=tmp_path, account=BANK_ACCOUNT, deb=str(deb))
    assert out["entries"] == 2
    assert out["balance"].split() == ["2026-09-30", "balance", BANK_ACCOUNT, "2140.13", "CNY"]
    staged = parse_text((tmp_path / "s.beancount").read_text("utf-8"))
    assert len(transactions(staged)) == 2 and len(staged) == 3
    assert [t.meta["via"] for t in transactions(staged)] == ["ccb", "ccb"]
    assert str(staged[-1].amount) == "2140.13 CNY"


def test_convert_bank_without_usable_balance_stages_none(tmp_path, make_fake_deb):
    deb, _ = make_fake_deb(bank_output("1.2E+06"))
    out = convert(SOURCES["ccb"], FIXTURES / "alipay/example-alipay-records.csv",
                  tmp_path / "s.beancount", settings=parse_settings({}), defaults_dir=DEFAULTS,
                  ledger_dir=tmp_path, account=BANK_ACCOUNT, deb=str(deb))
    assert out["entries"] == 1 and out["balance"] is None
    assert len(parse_text((tmp_path / "s.beancount").read_text("utf-8"))) == 1


def test_empty_output_is_an_error(tmp_path, fake_deb_empty):
    deb, _ = fake_deb_empty
    bill = tmp_path / "bill.csv"
    bill.write_bytes(b"x")
    with pytest.raises(LedgerError, match=r"no transactions in .*bill\.csv; is it a meituan export\?"):
        convert(SOURCES["meituan"], bill, tmp_path / "s.beancount", settings=parse_settings({}),
                defaults_dir=DEFAULTS, ledger_dir=tmp_path, account=None, deb=str(deb))
    assert not (tmp_path / "s.beancount").exists()


def test_converter_failure_shows_its_message(tmp_path, make_fake_deb):
    deb, _ = make_fake_deb(fail="Error: cannot read the bill: bad header")
    bill = tmp_path / "bill.csv"
    bill.write_bytes(b"x")
    with pytest.raises(LedgerError, match="cannot read the bill: bad header"):
        convert(SOURCES["jd"], bill, tmp_path / "s.beancount", settings=parse_settings({}),
                defaults_dir=DEFAULTS, ledger_dir=tmp_path, account=None, deb=str(deb))
    assert not (tmp_path / "s.beancount").exists()


def test_converter_panic_shows_the_panic_not_the_stack(tmp_path, make_fake_deb):
    trace = [
        "[Provider-Alipay] reading the bill",
        "panic: runtime error: index out of range [5] with length 1",
        "",
        "goroutine 1 [running]:",
        "main.main()",
        "\t/home/runner/main.go:21 +0xf",
    ]
    deb, _ = make_fake_deb(fail="\n".join(trace))
    bill = tmp_path / "bill.csv"
    bill.write_bytes(b"x")
    with pytest.raises(LedgerError) as raised:
        convert(SOURCES["alipay"], bill, tmp_path / "s.beancount", settings=parse_settings({}),
                defaults_dir=DEFAULTS, ledger_dir=tmp_path, account=None, deb=str(deb))
    assert str(raised.value).endswith("panic: runtime error: index out of range [5] with length 1")
    assert "goroutine" not in str(raised.value)


def test_missing_converter_and_missing_bill(tmp_path, fake_deb):
    deb, _ = fake_deb
    bill = tmp_path / "bill.csv"
    bill.write_bytes(b"x")
    common = {"settings": parse_settings({}), "defaults_dir": DEFAULTS, "ledger_dir": tmp_path,
              "account": None}
    with pytest.raises(LedgerError, match="no-such-converter is not installed"):
        convert(SOURCES["jd"], bill, tmp_path / "s.beancount", deb="no-such-converter", **common)
    with pytest.raises(LedgerError, match="no such file"):
        convert(SOURCES["jd"], tmp_path / "gone.csv", tmp_path / "s.beancount", deb=str(deb),
                **common)


def test_cli_convert(tmp_path, make_ledger, make_fake_deb, monkeypatch, capsys):
    deb, _ = make_fake_deb(name="double-entry-generator")
    monkeypatch.setenv("PATH", str(deb.parent) + os.pathsep + os.environ["PATH"])
    ledger = make_ledger()
    bill = tmp_path / "交易 明细.csv"
    bill.write_bytes(b"x")
    staged = tmp_path / "暂存" / "s.beancount"
    assert main(["convert", "--source", "alipay", "--file", str(bill), "--out", str(staged),
                 "--ledger-dir", str(ledger)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "staged": str(staged), "entries": 1, "balance": None}
    assert staged.exists()


def test_cli_convert_errors(tmp_path, make_ledger, make_fake_deb, monkeypatch, capsys):
    deb, _ = make_fake_deb(name="double-entry-generator")
    monkeypatch.setenv("PATH", str(deb.parent) + os.pathsep + os.environ["PATH"])
    ledger = make_ledger()
    bill = tmp_path / "bill.csv"
    bill.write_bytes(b"x")
    base = ["convert", "--file", str(bill), "--out", str(tmp_path / "s.beancount"),
            "--ledger-dir", str(ledger)]
    for extra, message in [
        (["--source", "ccb"], "lk: --account is required for bank statements"),
        (["--source", "nbcb", "--account", "Assets:Bank:NBCB:1"], "lk: nbcb has no converter"),
        (["--source", "nope"], "lk: unknown source nope"),
    ]:
        assert main(base + extra) == 1
        assert message in capsys.readouterr().err
    assert not (tmp_path / "s.beancount").exists()


def balanced(txn) -> bool:
    return sum(p.units.number for p in txn.postings) == 0


@pytest.mark.deb
@pytest.mark.parametrize("name,file,account", [
    ("alipay", "alipay/example-alipay-records.csv", None),
    ("wechat", "wechat/example-wechat-records.csv", None),
    ("meituan", "mt/example-mt-records.csv", None),
    ("jd", "jd/example-jd-records.csv", None),
    ("icbc", "icbc/example-icbc-debit-v2-records.csv", "Assets:Bank:ICBC:0001"),
])
def test_real_converter_on_examples(tmp_path, name, file, account):
    out = convert(SOURCES[name], FIXTURES / file, tmp_path / "s.beancount",
                  settings=parse_settings({}), defaults_dir=DEFAULTS, ledger_dir=tmp_path,
                  account=account)
    staged = parse_text((tmp_path / "s.beancount").read_text("utf-8"))
    assert out["entries"] > 0
    assert all(t.meta["via"] == name for t in transactions(staged))
    assert not any("FIXME" in p.account for t in transactions(staged) for p in t.postings)
    assert all(balanced(t) for t in transactions(staged))


@pytest.mark.deb
@pytest.mark.parametrize("name,file,accounts", [
    ("alipay", "alipay/example-alipay-records.csv", {
        "Assets:Alipay:Balance", "Assets:Alipay:YuEBao", "Expenses:Groceries"}),
    ("wechat", "wechat/example-wechat-records.csv", {
        "Assets:WeChat:Change", "Assets:WeChat:LingQianTong"}),
])
def test_real_converter_applies_the_default_rules(tmp_path, name, file, accounts):
    convert(SOURCES[name], FIXTURES / file, tmp_path / "s.beancount", settings=parse_settings({}),
            defaults_dir=DEFAULTS, ledger_dir=tmp_path, account=None)
    staged = parse_text((tmp_path / "s.beancount").read_text("utf-8"))
    used = {p.account for t in transactions(staged) for p in t.postings}
    assert accounts <= used


@pytest.mark.deb
def test_real_converter_applies_the_user_rules_last(tmp_path):
    (tmp_path / "importers").mkdir()
    rule = ["rules:", "  - method: 余额", "    fullMatch: true",
            "    methodAccount: Assets:Mine:Wallet", ""]
    (tmp_path / "importers/alipay.yaml").write_text("\n".join(rule), "utf-8")
    convert(SOURCES["alipay"], FIXTURES / "alipay/example-alipay-records.csv",
            tmp_path / "s.beancount", settings=parse_settings({}), defaults_dir=DEFAULTS,
            ledger_dir=tmp_path, account=None)
    staged = parse_text((tmp_path / "s.beancount").read_text("utf-8"))
    used = {p.account for t in transactions(staged) for p in t.postings}
    assert "Assets:Mine:Wallet" in used and "Assets:Alipay:Balance" not in used


@pytest.mark.deb
def test_real_converter_on_ccb_statement(tmp_path):
    account = "Assets:Bank:CCB:0001"
    (bill,) = (FIXTURES / "ccb").glob("*.xls")
    out = convert(SOURCES["ccb"], bill, tmp_path / "s.beancount", settings=parse_settings({}),
                  defaults_dir=DEFAULTS, ledger_dir=tmp_path, account=account)
    staged = parse_text((tmp_path / "s.beancount").read_text("utf-8"))
    txns = transactions(staged)
    assert out["entries"] == len(txns) > 0
    assert all(t.meta["via"] == "ccb" for t in txns)
    assert not any("FIXME" in p.account for t in txns for p in t.postings)
    assert all(account in {p.account for p in t.postings} for t in txns)
    # The directive is staged exactly when closing_balance finds one clear value.
    expected = closing_balance(txns, account, "CNY")
    assert (expected is None) == (out["balance"] is None) == (len(staged) == len(txns))
    # The example has two rows on its last day, so nothing is staged. With that day left out one
    # row ends the statement, and its balance is written the way closing_balance reads it.
    earlier = [t for t in txns if t.date < max(t2.date for t2 in txns)]
    balance = closing_balance(earlier, account, "CNY")
    assert (str(balance.date), str(balance.amount)) == ("2025-01-06", "1579.11 CNY")

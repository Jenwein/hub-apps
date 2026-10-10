import json
from datetime import date
from decimal import Decimal

import pytest
from ledgerkit.book import load
from ledgerkit.cli import main
from ledgerkit.report import money, net_worth, open_balances, period_totals, summary
from ledgerkit.settings import LedgerError

ACCTS = """\
2000-01-01 open Assets:Bank:CCB:1234
2000-01-01 open Assets:Fund
2000-01-01 open Assets:Receivables:Loans:ZhangSan
2000-01-01 open Liabilities:Payables:LiSi
2000-01-01 commodity FUNDA
2000-01-01 commodity FUNDB
"""


def txn(day, narration, *postings):
    lines = "".join(f"  {p}\n" for p in postings)
    return f'{day} * "Someone" "{narration}"\n{lines}\n'


BANK = "Assets:Bank:CCB:1234"
TXNS = (
    txn("2026-08-15", "lunch", "Expenses:Food  100.00 CNY", BANK)
    + txn("2026-09-03", "lunch", "Expenses:Food  35.00 CNY", BANK)
    + txn("2026-09-04", "lunch", "Expenses:Food  20.00 CNY", BANK)
    + txn("2026-09-05", "bus", "Expenses:Transport  20.00 CNY", BANK)
    + txn("2026-09-10", "salary", f"{BANK}  10000.00 CNY", "Income:Salary")
    + txn("2026-09-12", "lent", "Assets:Receivables:Loans:ZhangSan  500.00 CNY", BANK)
    + txn("2026-09-13", "borrowed", f"{BANK}  200.00 CNY", "Liabilities:Payables:LiSi")
    + txn("2026-09-20", "funds", "Assets:Fund  5 FUNDA {1.00 CNY}",
          "Assets:Fund  3 FUNDB {1.00 CNY}", f"{BANK}  -8.00 CNY")
    + "2026-09-30 price FUNDA 1.50 CNY\n"
)


@pytest.fixture
def ledger(make_ledger):
    return make_ledger(ACCTS, txns={2026: TXNS})


def entries_of(ledger):
    return load(ledger / "main.beancount")[0]


def test_month_summary(ledger):
    s = summary(ledger / "main.beancount", "2026-09", date(2026, 10, 2))
    assert s["expenses"] == {"Expenses:Food": "55.00", "Expenses:Transport": "20.00"}
    assert (s["expenses_total"], s["income_total"]) == ("75.00", "10000.00")
    assert s["previous"] == {"expenses_total": "100.00", "income_total": "0.00"}
    assert s["receivables"] == {"Assets:Receivables:Loans:ZhangSan": "500.00"}
    assert s["payables"] == {"Liabilities:Payables:LiSi": "-200.00"}
    assert s["net_worth"]["unpriced"] == ["FUNDB"]


def test_month_summary_values_and_key_order(ledger):
    s = summary(ledger / "main.beancount", "2026-09", date(2026, 10, 2))
    assert list(s) == [
        "currency", "month", "expenses", "expenses_total", "income", "income_total",
        "previous", "net_worth", "receivables", "payables", "unsorted"]
    assert (s["currency"], s["month"], s["income"], s["unsorted"]) == (
        "CNY", "2026-09", {"Income:Salary": "10000.00"}, 0)
    # Before 2026-09-01 only the August lunch has happened. At the end of September the bank holds
    # 9517.00, the loan 500.00, the debt -200.00 and 5 FUNDA 7.50; FUNDB has no price.
    assert s["net_worth"] == {"start": "-100.00", "end": "9824.50", "unpriced": ["FUNDB"]}


def test_summary_is_json_of_strings(ledger):
    s = summary(ledger / "main.beancount", "2026-09", date(2026, 10, 2))
    assert json.loads(json.dumps(s)) == s  # a Decimal in it would raise
    assert isinstance(s["expenses_total"], str) and isinstance(s["unsorted"], int)


def test_reminder_summary_has_only_balances(ledger):
    assert set(summary(ledger / "main.beancount", None, date(2026, 10, 1))) == {
        "currency", "receivables", "payables"}


def test_reminder_balances_are_as_of_the_end_of_today(ledger):
    def balances(today):
        s = summary(ledger / "main.beancount", None, today)
        return s["receivables"], s["payables"]

    assert balances(date(2026, 9, 11)) == ({}, {})
    assert balances(date(2026, 9, 12)) == ({"Assets:Receivables:Loans:ZhangSan": "500.00"}, {})
    assert balances(date(2026, 9, 13))[1] == {"Liabilities:Payables:LiSi": "-200.00"}


def test_paid_back_loan_is_not_listed(make_ledger):
    paid = txn("2026-09-25", "paid back", BANK + "  500.00 CNY",
               "Assets:Receivables:Loans:ZhangSan  -500.00 CNY")
    d = make_ledger(ACCTS, txns={2026: TXNS + paid})
    assert summary(d / "main.beancount", None, date(2026, 10, 1))["receivables"] == {}


def test_previous_month_of_january_is_december(make_ledger):
    d = make_ledger(ACCTS, txns={
        2025: txn("2025-12-31", "lunch", "Expenses:Food  12.30 CNY", BANK),
        2026: txn("2026-01-01", "lunch", "Expenses:Food  4.00 CNY", BANK),
    })
    s = summary(d / "main.beancount", "2026-01", date(2026, 2, 1))
    assert s["previous"] == {"expenses_total": "12.30", "income_total": "0.00"}
    assert s["expenses"] == {"Expenses:Food": "4.00"}


def test_unsorted_counts_entries_that_still_need_the_user(make_ledger):
    d = make_ledger(ACCTS, txns={2026: TXNS + txn(
        "2026-08-20", "unknown", "Expenses:Unsorted  9.90 CNY", BANK) + txn(
        "2026-09-21", "unknown", "Expenses:Food  5.00 CNY", "Assets:Unmapped")})
    s = summary(d / "main.beancount", "2026-09", date(2026, 10, 2))
    assert s["unsorted"] == 2
    assert summary(d / "main.beancount", "2026-08", date(2026, 10, 2))["unsorted"] == 2
    assert load(d / "main.beancount")[1], "the placeholders are never opened, so beancount objects"
    assert "Expenses:Unsorted" not in s["expenses"]  # dated in August


def test_currency_comes_from_config(ledger):
    config = ledger / "config.toml"
    config.write_text(config.read_text("utf-8").replace('"CNY"', '"USD"'), "utf-8", newline="\n")
    s = summary(ledger / "main.beancount", "2026-09", date(2026, 10, 2))
    assert (s["currency"], s["expenses"], s["expenses_total"]) == ("USD", {}, "0.00")


@pytest.mark.parametrize("month", [
    "2026-9", "2026-13", "2026-00", "202609", "2026-09-01", "", "26-09", " 2026-09",
    "２０２６-09",  # full-width digits
    "0001-01",  # the month before it does not exist
    "9999-12",  # the month after it does not exist
])
def test_bad_month_is_a_ledger_error(ledger, month):
    with pytest.raises(LedgerError, match="YYYY-MM"):
        summary(ledger / "main.beancount", month, date(2026, 10, 2))


def test_summary_needs_main_beancount(ledger):
    (ledger / "main.beancount").unlink()
    with pytest.raises(LedgerError, match="no main.beancount"):
        summary(ledger / "main.beancount", "2026-09", date(2026, 10, 2))


def test_money_has_two_decimals_and_no_negative_zero():
    assert money(Decimal(5)) == "5.00"
    assert money(Decimal(-200)) == "-200.00"
    assert money(Decimal("1E+3")) == "1000.00"
    assert money(Decimal("-0.001")) == "0.00"
    assert money(Decimal("0.125")) == "0.12"


# period_totals

TOTALS_ACCTS = ACCTS + """\
2000-01-01 open Expenses:Food:Lunch
2000-01-01 open Expenses:Food:Dinner
2000-01-01 open Income:Salary:Base
2000-01-01 open Income:Salary:Bonus
2000-01-01 open Assets:Usd
"""


def test_period_totals_group_by_two_components(make_ledger):
    d = make_ledger(TOTALS_ACCTS, txns={2026: (
        txn("2026-08-31", "before", "Expenses:Food  1.00 CNY", BANK)
        + txn("2026-09-01", "first day", "Expenses:Food:Lunch  10.00 CNY", BANK)
        + txn("2026-09-15", "dinner", "Expenses:Food:Dinner  5.50 CNY", BANK)
        + txn("2026-09-16", "refund", "Expenses:Food:Lunch  -3.00 CNY", BANK)
        + txn("2026-09-17", "taxi", "Expenses:Transport  8.00 CNY", BANK)
        + txn("2026-09-18", "dollars", "Expenses:Food  7.00 USD", "Assets:Usd")
        + txn("2026-10-01", "after", "Expenses:Food  99.00 CNY", BANK)
    )})
    totals = period_totals(entries_of(d), "Expenses", date(2026, 9, 1), date(2026, 10, 1), "CNY")
    assert totals == {"Expenses:Food": Decimal("12.50"), "Expenses:Transport": Decimal("8.00")}
    assert list(totals) == ["Expenses:Food", "Expenses:Transport"]  # the larger first
    usd = period_totals(entries_of(d), "Expenses", date(2026, 9, 1), date(2026, 10, 1), "USD")
    assert usd == {"Expenses:Food": Decimal("7.00")}


def test_period_totals_negate_income(make_ledger):
    d = make_ledger(TOTALS_ACCTS, txns={2026: (
        txn("2026-09-10", "pay", f"{BANK}  9000.00 CNY", "Income:Salary:Base")
        + txn("2026-09-11", "bonus", f"{BANK}  1000.00 CNY", "Income:Salary:Bonus")
        + txn("2026-09-12", "gift", f"{BANK}  50.00 CNY", "Income:Gifts")
        + txn("2026-09-13", "lunch", "Expenses:Food  1.00 CNY", BANK)
    )})
    totals = period_totals(entries_of(d), "Income", date(2026, 9, 1), date(2026, 10, 1), "CNY")
    assert totals == {"Income:Salary": Decimal("10000.00"), "Income:Gifts": Decimal("50.00")}


def test_period_totals_of_nothing(ledger):
    assert period_totals(entries_of(ledger), "Income", date(2026, 8, 1), date(2026, 9, 1), "CNY") == {}


# net_worth

def test_net_worth_values_prices_and_lists_unpriced(ledger):
    entries = entries_of(ledger)
    assert net_worth(entries, date(2026, 10, 1), "CNY") == (Decimal("9824.50"), ["FUNDB"])
    assert net_worth(entries, date(2026, 9, 1), "CNY") == (Decimal("-100.00"), [])
    # "before" the day: the funds bought on the 20th are not in yet.
    assert net_worth(entries, date(2026, 9, 20), "CNY")[1] == []
    assert net_worth(entries, date(2026, 9, 21), "CNY")[1] == ["FUNDA", "FUNDB"]  # no price yet


def test_net_worth_uses_the_latest_price_on_or_before_the_day(make_ledger):
    d = make_ledger(ACCTS, txns={2026: TXNS + "2026-10-01 price FUNDA 2.00 CNY\n"
                                 + "2026-11-01 price FUNDA 3.00 CNY\n"})
    entries = entries_of(d)
    cash = Decimal("9517.00") + 500 - 200
    assert net_worth(entries, date(2026, 9, 29), "CNY") == (cash, ["FUNDA", "FUNDB"])
    for day, price in [((9, 30), "1.50"), ((10, 1), "2.00"), ((10, 31), "2.00"), ((11, 1), "3.00")]:
        assert net_worth(entries, date(2026, *day), "CNY")[0] == cash + 5 * Decimal(price)


def test_net_worth_leaves_out_a_fund_that_was_sold(make_ledger):
    sold = txn("2026-09-25", "sold", "Assets:Fund  -3 FUNDB {1.00 CNY}", f"{BANK}  3.00 CNY")
    d = make_ledger(ACCTS, txns={2026: TXNS + sold})
    assert net_worth(entries_of(d), date(2026, 10, 1), "CNY") == (Decimal("9827.50"), [])


EXCHANGE = txn(
    "2026-09-01", "exchange", "Assets:Usd  10.00 USD @ 7.00 CNY", f"{BANK}  -70.00 CNY")


def test_net_worth_leaves_out_cash_in_a_currency_without_a_price(make_ledger):
    # A price on the transaction is not a price directive.
    d = make_ledger(ACCTS + "2000-01-01 open Assets:Usd\n", txns={2026: EXCHANGE})
    assert net_worth(entries_of(d), date(2026, 10, 1), "CNY") == (Decimal("-70.00"), ["USD"])


def test_net_worth_converts_cash_in_a_currency_with_a_price(make_ledger):
    d = make_ledger(ACCTS + "2000-01-01 open Assets:Usd\n", txns={
        2026: EXCHANGE + "2026-09-02 price USD 7.20 CNY\n"})
    assert net_worth(entries_of(d), date(2026, 10, 1), "CNY") == (Decimal("2.00"), [])


# open_balances

def test_open_balances(ledger):
    entries = entries_of(ledger)
    prefixes = ("Assets:Receivables:", "Liabilities:Payables:")
    assert open_balances(entries, prefixes, date(2026, 10, 1)) == {
        "Assets:Receivables:Loans:ZhangSan": Decimal("500.00"),
        "Liabilities:Payables:LiSi": Decimal("-200.00")}
    assert open_balances(entries, prefixes, date(2026, 9, 13)) == {
        "Assets:Receivables:Loans:ZhangSan": Decimal("500.00")}  # before the day
    assert open_balances(entries, ("Assets:Receivables:",), date(2026, 9, 12)) == {}
    assert open_balances(entries, ("Assets:Fund",), date(2026, 10, 1), currency="FUNDA") == {
        "Assets:Fund": Decimal(5)}


def test_open_balances_by_currency(make_ledger):
    d = make_ledger(ACCTS + "2000-01-01 open Assets:Receivables:Loans:Wang\n", txns={2026: (
        txn("2026-09-01", "cny", "Assets:Receivables:Loans:Wang  10.00 CNY", BANK)
        + txn("2026-09-02", "usd", "Assets:Receivables:Loans:Wang  3.00 USD", BANK + "  -3.00 USD")
    )})
    entries = entries_of(d)
    prefixes = ("Assets:Receivables:",)
    assert open_balances(entries, prefixes, date(2026, 10, 1), currency="USD") == {
        "Assets:Receivables:Loans:Wang": Decimal("3.00")}
    assert open_balances(entries, prefixes, date(2026, 10, 1), currency="CNY") == {
        "Assets:Receivables:Loans:Wang": Decimal("10.00")}
    with pytest.raises(LedgerError, match="several currencies"):
        open_balances(entries, prefixes, date(2026, 10, 1))


# command line

def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_summary_month(ledger, capsys, monkeypatch):
    class Today(date):
        @classmethod
        def today(cls):
            return date(2026, 10, 2)

    monkeypatch.setattr("ledgerkit.cli.date", Today)
    code, out, err = run(capsys, "summary", "--ledger-dir", str(ledger), "--month", "2026-09")
    assert (code, err) == (0, "")
    assert json.loads(out) == summary(ledger / "main.beancount", "2026-09", date(2026, 10, 2))
    assert '"expenses_total": "75.00"' in out


def test_cli_summary_without_month_is_the_reminder(ledger, capsys, monkeypatch):
    class Today(date):
        @classmethod
        def today(cls):
            return date(2026, 10, 1)

    monkeypatch.setattr("ledgerkit.cli.date", Today)
    code, out, _ = run(capsys, "summary", "--ledger-dir", str(ledger))
    assert code == 0 and json.loads(out) == {
        "currency": "CNY",
        "receivables": {"Assets:Receivables:Loans:ZhangSan": "500.00"},
        "payables": {"Liabilities:Payables:LiSi": "-200.00"},
    }


def test_cli_summary_bad_month_exits_1(ledger, capsys):
    code, out, err = run(capsys, "summary", "--ledger-dir", str(ledger), "--month", "2026-9")
    assert (code, out) == (1, "") and err.startswith("lk: ") and "YYYY-MM" in err

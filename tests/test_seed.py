import argparse
import tomllib
from pathlib import Path

import pytest
from beancount.core.data import Open
from beancount.loader import load_file as load
from ledgerkit import cli
from ledgerkit.cli import main
from ledgerkit.settings import (
    SOURCES,
    LedgerError,
    Source,
    load_settings,
    parse_settings,
    source,
)

APP = Path(__file__).parents[1] / "ledger"


def test_seed_ledger_loads_without_errors(make_ledger):
    _, errors, options = load(make_ledger() / "main.beancount")
    assert errors == []
    assert options["operating_currency"] == ["CNY"]


def test_seed_opens_no_placeholder(make_ledger):
    text = (make_ledger() / "accounts.beancount").read_text("utf-8")
    for acct in ("Expenses:Unsorted", "Income:Unsorted", "Assets:Unmapped"):
        assert acct not in text


def test_app_toml_fields():
    data = tomllib.loads((APP / "app.toml").read_text("utf-8"))
    assert data["app"]["name"] == "ledger" and data["app"]["min_hub"] == "0.4.0"
    assert data["data"]["dirs"] == ["ledger"]
    assert {r["bin"] for r in data["requires"]} == {
        "fava", "bean-check", "bean-query", "bean-price", "double-entry-generator", "uv"}


def test_settings_defaults_and_config():
    s = parse_settings({})
    assert (s.currency, s.start, s.window_days, s.fava_port) == ("CNY", None, 3, 5000)
    assert "财付通" in s.channel_keywords
    s = parse_settings({"chat": {"default": "Assets:Cash",
                                 "channels": {"wechat": "Assets:Bank:CCB:1234"}},
                        "sources": {"cmbc": {"kind": "bank"}}})
    assert s.chat_channels["wechat"] == "Assets:Bank:CCB:1234"
    assert source(s, "cmbc") == Source("cmbc", "bank", None)
    with pytest.raises(LedgerError):
        source(s, "nope")


def test_seed_config_parses(make_ledger):
    assert load_settings(make_ledger()).sources == ()


def test_seed_config_matches_defaults(make_ledger):
    assert load_settings(make_ledger()) == parse_settings({})


def test_seed_gitignore_ignores_inbox(make_ledger):
    assert (make_ledger() / ".gitignore").read_bytes() == b"inbox/\n"


def test_missing_config_is_an_error(tmp_path):
    with pytest.raises(LedgerError):
        load_settings(tmp_path)


def test_builtin_sources():
    assert SOURCES["wechat"].extra_args == ("--ignore-invalid-tx-types",)
    assert all(s.extra_args == () for n, s in SOURCES.items() if n != "wechat")
    assert {n for n, s in SOURCES.items() if s.provider is None} == {"bosc", "nbcb"}
    assert SOURCES["meituan"] == Source("meituan", "platform", "mt")


def test_blank_start_is_none_and_start_is_kept():
    assert parse_settings({"ledger": {"start": ""}}).start is None
    assert parse_settings({"ledger": {"start": "2026-01-01"}}).start == "2026-01-01"


def test_unknown_source_message():
    with pytest.raises(LedgerError, match="unknown source nope"):
        source(parse_settings({}), "nope")


def test_cli_without_a_command_prints_usage_and_fails(capsys):
    with pytest.raises(SystemExit) as stop:
        main([])
    assert stop.value.code != 0
    assert "usage" in capsys.readouterr().err


def test_ledger_error_becomes_exit_1(monkeypatch, capsys):
    def fail(args):
        raise LedgerError("it broke")

    parser = argparse.ArgumentParser()
    parser.set_defaults(func=fail)
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    assert main([]) == 1
    assert capsys.readouterr().err == "lk: it broke\n"


def test_seed_opens_the_default_accounts(make_ledger):
    entries, _, _ = load(make_ledger() / "main.beancount")
    opened = {e.account for e in entries if isinstance(e, Open)}
    expenses = ["Food", "Groceries", "Transport", "Shopping", "Housing", "Utilities", "Communication", "Health", "Entertainment", "Travel", "Education", "Gifts", "Insurance", "Fees", "Other"]
    income = ["Salary", "Bonus", "Interest", "Investment", "Gifts", "Other"]
    assert opened == {
        "Equity:Opening-Balances", "Assets:Cash", "Assets:Receivables:Reimbursement",
        *(f"Expenses:{name}" for name in expenses), *(f"Income:{name}" for name in income),
    }


def test_extra_source_may_name_a_provider():
    s = parse_settings({"sources": {"huobi": {"kind": "platform", "provider": "huobi"}}})
    assert source(s, "huobi") == Source("huobi", "platform", "huobi")
    assert source(s, "alipay") is SOURCES["alipay"]


@pytest.mark.parametrize("data", [
    {"ledger": {"start": "2026-13-01"}},
    {"ledger": {"start": "20260101"}},
    {"import": {"window_days": "3"}},
    {"import": {"sources": "alipay"}},
    {"fava": {"tailscale": "yes"}},
    {"sources": {"x": {"kind": "wallet"}}},
    {"sources": {"x": {}}},
    {"chat": "Assets:Cash"},
    {"chat": {"channels": {"wechat": 1}}},
])
def test_bad_config_is_a_ledger_error(data):
    with pytest.raises(LedgerError):
        parse_settings(data)


def test_toml_syntax_error_is_a_ledger_error(tmp_path):
    (tmp_path / "config.toml").write_text("[ledger\n", "utf-8")
    with pytest.raises(LedgerError):
        load_settings(tmp_path)

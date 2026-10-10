import codecs
import io
import json

import pytest
from beancount import loader
from ledgerkit.book import (
    PLACEHOLDERS,
    add_text,
    append_entries,
    digest,
    format_entry,
    funding_postings,
    guarded,
    is_transfer,
    load,
    parse_text,
    transactions,
    year_file,
)
from ledgerkit.cli import main
from ledgerkit.match import run
from ledgerkit.settings import LedgerError, parse_settings

ACCTS = "2000-01-01 open Assets:Bank:CCB:1234\n"
LUNCH = '2026-09-03 * "Canteen" "lunch"\n  Expenses:Food  35.00 CNY\n  Assets:Bank:CCB:1234\n'
BAD = LUNCH.replace("Expenses:Food", "Expenses:Nope")  # account never opened
TRANSFER = (
    '2026-09-04 * "Card" "repay"\n'
    "  Liabilities:Card  100.00 CNY\n  Assets:Bank:CCB:1234  -100.00 CNY\n"
)


def test_placeholders():
    assert PLACEHOLDERS == {"Expenses:Unsorted", "Income:Unsorted", "Assets:Unmapped"}


def test_add_writes_year_file_with_via_chat(make_ledger):
    d = make_ledger(ACCTS)
    assert add_text(d, LUNCH) == {"written": 1, "files": ["txns/2026.beancount"]}
    text = (d / "txns/2026.beancount").read_text("utf-8")
    assert text.startswith("; Transactions of 2026") and 'via: "chat"' in text
    assert load(d / "main.beancount")[1] == []


def test_add_across_year_boundary(make_ledger):
    d = make_ledger(ACCTS)
    add_text(
        d, LUNCH.replace("2026-09-03", "2025-12-31") + "\n" + LUNCH.replace("2026-09-03", "2026-01-01")
    )
    assert (d / "txns/2025.beancount").exists() and (d / "txns/2026.beancount").exists()
    entries, errors, _ = load(d / "main.beancount")
    assert errors == [] and len(transactions(entries)) == 2


def test_add_rolls_back_on_new_error(make_ledger):
    d = make_ledger(ACCTS)
    with pytest.raises(LedgerError, match="ledger check failed"):
        add_text(d, BAD)
    assert not (d / "txns/2026.beancount").exists()


def test_guarded_ignores_existing_errors(make_ledger):
    d = make_ledger(
        ACCTS,
        txns={2025: LUNCH.replace("2026-09-03", "2025-05-05").replace("Expenses:Food", "Expenses:Old")},
    )
    assert add_text(d, LUNCH)["written"] == 1  # the old error stays, no new one


def test_parse_error_is_ledger_error():
    # beancount reports the junk after the account as an invalid token.
    with pytest.raises(LedgerError, match="junk"):
        parse_text('2026-09-03 * "x"\n  Expenses:Food 1 CNY\n  Assets:X  junk\n')


def test_cli_add_reads_stdin(make_ledger, monkeypatch, capsys):
    d = make_ledger(ACCTS)
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(LUNCH.encode("utf-8"))))
    assert main(["add", "--ledger-dir", str(d)]) == 0
    assert json.loads(capsys.readouterr().out)["written"] == 1


def test_cli_add_accepts_a_utf8_bom(make_ledger, monkeypatch, capsys):
    d = make_ledger(ACCTS)
    stdin = codecs.BOM_UTF8 + LUNCH.encode()
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(stdin)))
    assert main(["add", "--ledger-dir", str(d)]) == 0
    assert json.loads(capsys.readouterr().out)["written"] == 1


def test_cli_add_reports_a_bad_entry(make_ledger, monkeypatch, capsys):
    d = make_ledger(ACCTS)
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(BAD.encode("utf-8"))))
    assert main(["add", "--ledger-dir", str(d)]) == 1
    assert capsys.readouterr().err.startswith("lk: ledger check failed")
    assert not (d / "txns/2026.beancount").exists()


def test_cli_add_rejects_non_utf8_stdin(make_ledger, monkeypatch, capsys):
    d = make_ledger(ACCTS)
    gbk = '2026-09-03 * "午餐"\n'.encode("gbk")
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(gbk)))
    assert main(["add", "--ledger-dir", str(d)]) == 1
    assert "UTF-8" in capsys.readouterr().err


def test_format_entry_keeps_a_posting_without_amount():
    text = format_entry(parse_text(LUNCH)[0])
    assert text.endswith("\n") and "\r" not in text
    assert text.splitlines()[-1] == "  Assets:Bank:CCB:1234"
    assert parse_text(text)[0].postings[1].account == "Assets:Bank:CCB:1234"


def test_chinese_text_is_written_as_utf8_with_lf(make_ledger):
    d = make_ledger(ACCTS)
    add_text(d, LUNCH.replace("Canteen", "食堂").replace("lunch", "午餐"))
    data = (d / "txns/2026.beancount").read_bytes()
    assert "食堂".encode() in data and b"\r" not in data


def test_add_keeps_a_given_via(make_ledger):
    d = make_ledger(ACCTS)
    add_text(d, LUNCH.replace('"lunch"\n', '"lunch"\n  via: "alipay"\n'))
    text = (d / "txns/2026.beancount").read_text("utf-8")
    assert 'via: "alipay"' in text and 'via: "chat"' not in text


def test_append_after_existing_entries(make_ledger):
    d = make_ledger(ACCTS, txns={2026: LUNCH.replace("lunch", "first").rstrip("\n")})  # no final LF
    add_text(d, LUNCH.replace("lunch", "second"))
    text = (d / "txns/2026.beancount").read_text("utf-8")
    assert text.index("first") < text.index("second")
    assert '"first"\n  Expenses' in text and "\n\n2026-09-03" in text
    assert load(d / "main.beancount")[1] == []


def test_funding_postings_and_transfer():
    lunch, transfer = parse_text(LUNCH)[0], parse_text(TRANSFER)[0]
    assert [p.account for p in funding_postings(lunch)] == ["Assets:Bank:CCB:1234"]
    assert [p.account for p in funding_postings(transfer)] == [
        "Liabilities:Card",
        "Assets:Bank:CCB:1234",
    ]
    assert is_transfer(transfer) and not is_transfer(lunch)


def test_year_file(tmp_path):
    assert year_file(tmp_path, 2026) == tmp_path / "txns" / "2026.beancount"


def test_load_ignores_the_pickle_cache(make_ledger, monkeypatch):
    # beancount's cache does not notice a new file matched by an include glob.
    monkeypatch.setattr(loader, "PICKLE_CACHE_THRESHOLD", 0.0)
    loader.initialize(use_cache=True)
    d = make_ledger(ACCTS)
    loader.load_file(str(d / "main.beancount"))
    assert (d / ".main.beancount.picklecache").exists()  # the cache is really in play
    year_file(d, 2026).write_text(LUNCH, "utf-8")
    assert len(transactions(load(d / "main.beancount")[0])) == 1


def snapshot_of(ledger):
    return {p: p.read_bytes() for p in ledger.rglob("*.beancount")}


def test_guarded_restores_files_on_new_error(make_ledger):
    d = make_ledger(ACCTS)
    before = snapshot_of(d)
    with pytest.raises(LedgerError, match="Expenses:Nope"), guarded(d):
        with open(d / "txns/web.beancount", "a", encoding="utf-8", newline="\n") as out:
            out.write("\n" + BAD)  # changes an existing file
        year_file(d, 2026).write_text(BAD, "utf-8")  # and creates one
    assert snapshot_of(d) == before


def test_guarded_restores_files_on_exception(make_ledger):
    d = make_ledger(ACCTS)
    before = snapshot_of(d)
    with pytest.raises(RuntimeError, match="boom"), guarded(d):
        (d / "accounts.beancount").write_text("", "utf-8")
        (d / "txns/opening.beancount").unlink()
        year_file(d, 2026).write_text(LUNCH, "utf-8")
        raise RuntimeError("boom")
    assert snapshot_of(d) == before


def test_guarded_keeps_a_clean_write(make_ledger):
    d = make_ledger(ACCTS)
    with guarded(d):
        append_entries(d, parse_text(LUNCH))
    assert year_file(d, 2026).exists()


def test_guarded_needs_a_ledger(tmp_path):
    with pytest.raises(LedgerError, match="no main.beancount"), guarded(tmp_path / "nowhere"):
        pass
    assert not (tmp_path / "nowhere").exists()


def test_digest_covers_the_ledger_files_but_the_inbox(make_ledger):
    d = make_ledger(ACCTS)
    first = digest(d)
    assert len(first) == 64 and digest(d) == first
    inbox = d / "inbox" / "20261001-093000-alipay"
    inbox.mkdir(parents=True)
    (inbox / "staged.beancount").write_text(LUNCH, "utf-8")
    (d / "documents" / "bill.beancount.txt").write_text("x", "utf-8")
    assert digest(d) == first  # the inbox and files that are not .beancount do not count
    year_file(d, 2026).write_text(LUNCH, "utf-8")
    second = digest(d)
    assert second != first
    (d / "txns" / "2026.beancount").rename(d / "txns" / "2027.beancount")
    assert digest(d) not in (first, second)  # a path counts, not only the bytes


INTEREST = (
    '2026-09-30 * "LingQianTong" "interest"\n'
    "  Assets:WeChat:Change  5.00 CNY\n  Income:Interest  -5.00 CNY\n"
)


def test_add_with_another_via(make_ledger, monkeypatch, capsys):
    d = make_ledger("2000-01-01 open Assets:WeChat:Change\n")
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(INTEREST.encode("utf-8"))))
    assert main(["add", "--ledger-dir", str(d), "--via", "ledger"]) == 0
    capsys.readouterr()
    text = (d / "txns/2026.beancount").read_text("utf-8")
    assert 'via: "ledger"' in text and 'via: "chat"' not in text
    # Matching never takes it for a payment told in chat: a bill row is not folded into it.
    staged = d.parent / "staged.beancount"
    staged.write_text(
        '2026-09-30 * "李四" "转账"\n  via: "wechat"\n  orderId: "W60"\n'
        "  Assets:WeChat:Change  5.00 CNY\n  Income:Gifts  -5.00 CNY\n", "utf-8")
    summary = run(d / "main.beancount", staged, d.parent / "plan.json", parse_settings({}))
    assert (summary["fix"], summary["uncertain"]) == (0, 1)


@pytest.mark.parametrize("via", ["", "Ledger", "my ledger"])
def test_add_refuses_a_bad_via(make_ledger, via):
    d = make_ledger(ACCTS)
    with pytest.raises(LedgerError, match="via"):
        add_text(d, LUNCH, via=via)
    assert not (d / "txns/2026.beancount").exists()

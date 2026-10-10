import json
import os
import struct
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

import matplotlib
import pytest
from ledgerkit import chart as chart_module
from ledgerkit.chart import (
    FONT_CANDIDATES,
    chart,
    chart_title,
    month_windows,
    networth_dates,
    pick_font,
)
from ledgerkit.cli import main
from ledgerkit.settings import LedgerError

# A matplotlib warning (a missing glyph, a deprecation) fails the test.
pytestmark = pytest.mark.filterwarnings("error")

ACCTS = """\
2000-01-01 open Assets:Bank:CCB:1234
2000-01-01 open Assets:Fund
2000-01-01 open Assets:Receivables:Loans:ZhangSan
2000-01-01 open Liabilities:Payables:LiSi
2000-01-01 commodity FUNDA
2000-01-01 commodity FUNDB
"""
BANK = "Assets:Bank:CCB:1234"


def txn(day, narration, *postings):
    lines = "".join(f"  {p}\n" for p in postings)
    return f'{day} * "Someone" "{narration}"\n{lines}\n'


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
NO_FONT = ["no CJK font found; install one such as fonts-noto-cjk"]
PNG = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def ledger(make_ledger):
    return make_ledger(ACCTS, txns={2026: TXNS})


def size_of(path):
    data = Path(path).read_bytes()
    assert data[:8] == PNG
    return struct.unpack(">II", data[16:24])


def test_font_candidates():
    assert FONT_CANDIDATES == (
        "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Zen Hei", "Microsoft YaHei",
        "PingFang SC", "SimHei")


def test_pick_font():
    assert pick_font(["Food"], set()) == (None, [])
    assert pick_font(["餐饮"], {"SimHei"}) == ("SimHei", [])
    font, warnings = pick_font(["餐饮"], set())
    assert font is None and "fonts-noto-cjk" in warnings[0]


def test_pick_font_takes_the_first_candidate_in_order():
    assert pick_font(["Food", "餐饮"], {"SimHei", "Microsoft YaHei", "Arial"}) == (
        "Microsoft YaHei", [])
    assert pick_font([], set()) == (None, [])
    assert pick_font(["Food", "Rent"], {"SimHei"}) == (None, [])


def test_pick_font_defaults_to_the_fonts_matplotlib_knows():
    font, warnings = pick_font(["餐饮"])
    assert (font in FONT_CANDIDATES and warnings == []) or (font is None and warnings == NO_FONT)
    assert pick_font(["Food"]) == (None, [])


@pytest.mark.parametrize("kind", ["expenses", "trend", "networth"])
def test_chart_writes_png(ledger, tmp_path, kind):
    r = chart(kind, ledger / "main.beancount", date(2026, 8, 1), date(2026, 10, 1),
              tmp_path / f"{kind}.png", None)
    assert Path(r["path"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert Path(r["path"]) == tmp_path / f"{kind}.png"
    width, height = size_of(r["path"])
    assert width > 400 and height > 200
    # networth has FUNDB, which has no price
    assert bool(r["warnings"]) == (kind == "networth")


def test_networth_warns_about_unpriced_currencies(ledger, tmp_path):
    r = chart("networth", ledger / "main.beancount", date(2026, 9, 1), date(2026, 10, 1),
              tmp_path / "n.png", None)
    assert len(r["warnings"]) == 1 and "FUNDB" in r["warnings"][0]


def test_chart_creates_the_folder_of_out(ledger, tmp_path):
    out = tmp_path / "new" / "folder" / "e.png"
    r = chart("expenses", ledger / "main.beancount", date(2026, 9, 1), date(2026, 10, 1), out, None)
    assert out.is_file() and r == {"path": str(out), "warnings": []}


def test_default_out_is_a_new_file_in_the_temp_dir(ledger, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    args = ("expenses", ledger / "main.beancount", date(2026, 9, 1), date(2026, 10, 1), None, None)
    first, second = chart(*args), chart(*args)
    assert first["path"] != second["path"]
    for r in (first, second):
        assert Path(r["path"]).parent == tmp_path and size_of(r["path"])[0] > 400


def test_chart_closes_its_figures(ledger, tmp_path):
    import matplotlib.pyplot as plt

    chart("trend", ledger / "main.beancount", date(2026, 8, 1), date(2026, 10, 1),
          tmp_path / "t.png", None)
    assert plt.get_fignums() == []


def test_chart_uses_the_agg_backend(ledger, tmp_path):
    chart("expenses", ledger / "main.beancount", date(2026, 9, 1), date(2026, 10, 1),
          tmp_path / "e.png", None)
    assert matplotlib.get_backend().lower() == "agg"


def test_chinese_title_without_font_falls_back(ledger, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "ledgerkit.chart.pick_font",
        lambda texts, available=None: (None, ["no CJK font found; install one such as fonts-noto-cjk"]))
    r = chart("expenses", ledger / "main.beancount", date(2026, 9, 1), date(2026, 10, 1),
              tmp_path / "e.png", "九月支出")
    assert r["warnings"] and Path(r["path"]).exists()


def test_title_choice():
    start, end = date(2026, 9, 1), date(2026, 10, 1)
    default = chart_title("expenses", start, end, None, None)
    assert default.isascii() and "2026-09-01" in default and "2026-09-30" in default
    assert chart_title("trend", start, end, None, None) != default
    assert chart_title("networth", start, end, None, None) != default
    assert chart_title("expenses", start, end, "September", None) == "September"
    assert chart_title("expenses", start, end, "九月支出", None) == default  # nothing can draw it
    assert chart_title("expenses", start, end, "九月支出", "SimHei") == "九月支出"


def test_pick_font_sees_the_title_and_the_labels(ledger, tmp_path, monkeypatch):
    seen = []

    def spy(texts, available=None):
        seen.append(list(texts))
        return None, NO_FONT

    monkeypatch.setattr("ledgerkit.chart.pick_font", spy)
    chart("expenses", ledger / "main.beancount", date(2026, 9, 1), date(2026, 10, 1),
          tmp_path / "e.png", "九月支出")
    assert len(seen) == 1 and "九月支出" in seen[0] and "Food" in seen[0]


def test_non_ascii_labels_without_font_warn_and_still_draw(make_ledger, tmp_path, monkeypatch):
    d = make_ledger(ACCTS + "2000-01-01 open Expenses:餐饮\n", txns={2026: txn(
        "2026-09-03", "lunch", "Expenses:餐饮  35.00 CNY", BANK)})
    monkeypatch.setattr("ledgerkit.chart.pick_font", lambda texts, available=None: (None, NO_FONT))
    r = chart("expenses", d / "main.beancount", date(2026, 9, 1), date(2026, 10, 1),
              tmp_path / "e.png", None)  # matplotlib's own missing-glyph warnings stay quiet
    assert r["warnings"] == NO_FONT and size_of(r["path"])[0] > 400


def test_font_is_set_for_that_figure_only(ledger, tmp_path, monkeypatch):
    before = dict(matplotlib.rcParams)
    monkeypatch.setattr("ledgerkit.chart.pick_font", lambda texts, available=None: ("DejaVu Sans", []))
    chart("expenses", ledger / "main.beancount", date(2026, 9, 1), date(2026, 10, 1),
          tmp_path / "e.png", "Café costs")
    assert dict(matplotlib.rcParams) == before


def test_a_real_cjk_font_draws_chinese(make_ledger, tmp_path):
    font, _ = pick_font(["餐饮"])
    if font is None:
        pytest.skip("no CJK font is installed")
    d = make_ledger(ACCTS + "2000-01-01 open Expenses:餐饮\n", txns={2026: txn(
        "2026-09-03", "lunch", "Expenses:餐饮  35.00 CNY", BANK) + txn(
        "2026-09-04", "lunch", "Expenses:Food  45.00 CNY", BANK)})
    for kind in ("expenses", "trend", "networth"):
        r = chart(kind, d / "main.beancount", date(2026, 9, 1), date(2026, 10, 1),
                  tmp_path / f"{kind}.png", "九月")
        assert r["warnings"] == [] and size_of(r["path"])[0] > 400


def test_unknown_kind(ledger, tmp_path):
    with pytest.raises(LedgerError, match="expenses, trend, networth"):
        chart("pie", ledger / "main.beancount", date(2026, 9, 1), date(2026, 10, 1),
              tmp_path / "x.png", None)
    assert not (tmp_path / "x.png").exists()


@pytest.mark.parametrize("start, end", [
    (date(2026, 10, 1), date(2026, 10, 1)), (date(2026, 10, 2), date(2026, 10, 1))])
def test_start_must_be_before_end(ledger, tmp_path, start, end):
    with pytest.raises(LedgerError, match="before"):
        chart("expenses", ledger / "main.beancount", start, end, tmp_path / "x.png", None)


@pytest.mark.parametrize("kind", ["expenses", "trend"])
def test_nothing_to_draw_is_an_error(ledger, tmp_path, kind):
    with pytest.raises(LedgerError, match="nothing to chart"):
        chart(kind, ledger / "main.beancount", date(2025, 1, 1), date(2025, 6, 1),
              tmp_path / "x.png", None)
    assert not (tmp_path / "x.png").exists()


def test_chart_needs_main_beancount(ledger, tmp_path):
    (ledger / "main.beancount").unlink()
    with pytest.raises(LedgerError, match="no main.beancount"):
        chart("expenses", ledger / "main.beancount", date(2026, 9, 1), date(2026, 10, 1),
              tmp_path / "x.png", None)


def test_month_windows_clip_to_the_range():
    assert month_windows(date(2026, 8, 15), date(2026, 10, 1)) == [
        (date(2026, 8, 15), date(2026, 9, 1)), (date(2026, 9, 1), date(2026, 10, 1))]
    assert month_windows(date(2026, 9, 1), date(2026, 9, 10)) == [(date(2026, 9, 1), date(2026, 9, 10))]
    assert month_windows(date(2025, 12, 1), date(2026, 2, 1)) == [
        (date(2025, 12, 1), date(2026, 1, 1)), (date(2026, 1, 1), date(2026, 2, 1))]


def test_networth_dates_are_the_ends_and_the_month_starts_between():
    assert networth_dates(date(2026, 8, 1), date(2026, 10, 1)) == [
        date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1)]
    assert networth_dates(date(2026, 8, 15), date(2026, 9, 10)) == [
        date(2026, 8, 15), date(2026, 9, 1), date(2026, 9, 10)]
    assert networth_dates(date(2026, 9, 3), date(2026, 9, 20)) == [date(2026, 9, 3), date(2026, 9, 20)]


# command line

def test_cli_chart(ledger, tmp_path, capsys):
    out = tmp_path / "e.png"
    code = main(["chart", "expenses", "--ledger-dir", str(ledger), "--start", "2026-09-01",
                 "--end", "2026-10-01", "--out", str(out), "--title", "September"])
    printed = capsys.readouterr()
    assert (code, printed.err) == (0, "")
    assert json.loads(printed.out) == {"path": str(out), "warnings": []}
    assert size_of(out)[0] > 400


def test_cli_chart_bad_arguments_exit_1(ledger, tmp_path, capsys):
    base = ["chart", "expenses", "--ledger-dir", str(ledger)]
    for extra, message in (
        (["--start", "2026-9-1", "--end", "2026-10-01"], "--start"),
        (["--start", "2026-09-01", "--end", "20261001"], "--end"),
        (["--start", "2026-09-01", "--end", "2026-09-01"], "before"),
    ):
        code = main(base + extra + ["--out", str(tmp_path / "x.png")])
        printed = capsys.readouterr()
        assert (code, printed.out) == (1, "")
        assert printed.err.startswith("lk: ") and message in printed.err
    assert not (tmp_path / "x.png").exists()


def test_report_and_cli_do_not_import_matplotlib():
    code = (
        "import sys; import ledgerkit.report, ledgerkit.cli; "
        "sys.exit('matplotlib' in sys.modules)")
    scripts = Path(chart_module.__file__).parents[1]
    done = subprocess.run(
        [sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": str(scripts)}, check=False)
    assert done.returncode == 0


def test_a_range_of_centuries_is_refused(ledger, tmp_path):
    with pytest.raises(LedgerError, match="more than 600 months"):
        month_windows(date(1500, 1, 1), date(2026, 1, 1))
    with pytest.raises(LedgerError, match="more than 600 months"):
        chart("trend", ledger / "main.beancount", date(26, 1, 1), date(2026, 1, 1),
              tmp_path / "x.png", None)
    assert month_windows(date(9999, 11, 15), date(9999, 12, 31)) == [
        (date(9999, 11, 15), date(9999, 12, 1)), (date(9999, 12, 1), date(9999, 12, 31))]


def test_an_unwritable_out_is_a_ledger_error(ledger, tmp_path):
    with pytest.raises(LedgerError, match="cannot write the chart"):
        chart("expenses", ledger / "main.beancount", date(2026, 9, 1), date(2026, 10, 1),
              tmp_path, None)  # a folder

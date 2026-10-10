"""Charts of the ledger as PNG files, for the agent to send to the chat.

Three kinds: `expenses` (bars by category), `trend` (income and expenses per month) and `networth`
(a line over the months). The numbers come from `report`, which imports no matplotlib; this module
loads matplotlib on the first chart, so that the other commands start fast.
"""

from __future__ import annotations

import io
import os
import tempfile
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from . import report
from .settings import LedgerError

FONT_CANDIDATES = (
    "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Zen Hei", "Microsoft YaHei",
    "PingFang SC", "SimHei")
NO_FONT = "no CJK font found; install one such as fonts-noto-cjk"
KINDS = ("expenses", "trend", "networth")
# A chart of months with more points than this is a typo in the dates, not a wish.
MAX_MONTHS = 600

# Colors from the dataviz palette on its light surface: the first two categorical slots, which
# pass the color checks (CVD, lightness, contrast) for two series, and the text and grid inks.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
MUTED = "#52514e"
GRID = "#e6e5e1"
BLUE = "#2a78d6"
ORANGE = "#eb6834"
DPI = 150


def pick_font(texts: list[str], available: set[str] | None = None) -> tuple[str | None, list[str]]:
    """The font that can draw `texts`, and warnings.

    Text that is all ASCII needs none: (None, []). Otherwise the first of FONT_CANDIDATES among
    the `available` font family names (by default those matplotlib knows), or (None, [warning]).
    """
    if all(text.isascii() for text in texts):
        return None, []
    if available is None:
        from matplotlib import font_manager

        available = {font.name for font in font_manager.fontManager.ttflist}
    for candidate in FONT_CANDIDATES:
        if candidate in available:
            return candidate, []
    return None, [NO_FONT]


def default_title(kind: str, start: date, end: date) -> str:
    """The English title of a chart. The range shown is `start` to the day before `end`."""
    what = {
        "expenses": "Expenses by category",
        "trend": "Income and expenses by month",
        "networth": "Net worth",
    }[kind]
    return f"{what}, {start.isoformat()} to {(end - timedelta(days=1)).isoformat()}"


def chart_title(kind: str, start: date, end: date, title: str | None, font: str | None) -> str:
    """`title`, or the default one when there is none or no font can draw it."""
    if title and (font or title.isascii()):
        return title
    return default_title(kind, start, end)


def _month_starts(start: date, end: date) -> Iterator[date]:
    """The first days of the months from the month of `start` up to before `end`."""
    day = report.shift_month(start, 0)
    count = 0
    while day < end:
        count += 1
        if count > MAX_MONTHS:
            raise LedgerError(f"{start} up to {end} is more than {MAX_MONTHS} months; check the dates")
        yield day
        try:
            day = report.shift_month(day, 1)
        except ValueError:  # past the last year that dates have
            return


def month_windows(start: date, end: date) -> list[tuple[date, date]]:
    """The months from `start` up to `end` as (from, up to) pairs, the first and last clipped."""
    firsts = list(_month_starts(start, end))
    return [(max(first, start), stop) for first, stop in zip(firsts, firsts[1:] + [end])]


def networth_dates(start: date, end: date) -> list[date]:
    """The days to take the net worth on: `start`, the month starts after it, and `end`."""
    between = [first for first in _month_starts(start, end) if first > start]
    return [start, *between, end]


def _pyplot():
    import matplotlib

    matplotlib.use("Agg")  # no window, whatever the machine; before pyplot is imported
    import matplotlib.pyplot as plt

    return plt


def _amount(value: Decimal) -> str:
    return f"{Decimal(report.money(value)):,.2f}"


def _sum(totals: dict[str, Decimal]) -> Decimal:
    return sum(totals.values(), Decimal(0))


@dataclass
class Plan:
    """What a kind of chart needs: texts to draw, notes for the caller, a size, and a drawing."""

    labels: list[str]
    size: tuple[float, float]
    draw: Callable  # draw(ax) fills the axes
    notes: list[str] = field(default_factory=list)


def _expenses(entries, start: date, end: date, currency: str) -> Plan:
    totals = report.period_totals(entries, "Expenses", start, end, currency)
    # What was refunded beyond what was spent has no bar.
    totals = {account: number for account, number in totals.items() if number > 0}
    if not totals:
        raise LedgerError(f"nothing to chart: no expenses from {start} up to {end}")
    labels = [account.partition(":")[2] for account in totals]

    def draw(ax):
        numbers = [float(number) for number in totals.values()]
        rows = range(len(numbers))
        ax.barh(rows, numbers, height=0.6, color=BLUE)
        ax.set_yticks(rows, labels)
        ax.invert_yaxis()  # the largest on top
        ax.set_xlim(0, max(numbers) * 1.2)
        for row, number in zip(rows, totals.values()):
            ax.text(float(number), row, "  " + _amount(number), va="center", color=MUTED,
                    fontsize=9)
        ax.set_xlabel(currency)
        ax.xaxis.grid(True)
        ax.tick_params(axis="y", length=0)

    return Plan(labels, (8, max(2.8, 0.5 * len(labels) + 1.6)), draw)


def _trend(entries, start: date, end: date, currency: str) -> Plan:
    windows = month_windows(start, end)
    income = [_sum(report.period_totals(entries, "Income", *w, currency)) for w in windows]
    expenses = [_sum(report.period_totals(entries, "Expenses", *w, currency)) for w in windows]
    if not any(income) and not any(expenses):
        raise LedgerError(f"nothing to chart: no income or expenses from {start} up to {end}")
    names = [first.strftime("%Y-%m") for first, _ in windows]

    def draw(ax):
        rows = range(len(names))
        width = 0.38
        ax.bar([row - width / 2 for row in rows], [float(n) for n in income], width,
               color=BLUE, label="Income")
        ax.bar([row + width / 2 for row in rows], [float(n) for n in expenses], width,
               color=ORANGE, label="Expenses")
        crowded = len(names) > 6
        ax.set_xticks(rows, names, rotation=45 if crowded else 0, ha="right" if crowded else "center")
        ax.set_ylabel(currency)
        ax.yaxis.grid(True)
        ax.legend(loc="upper left", ncol=2)

    return Plan(names, (min(16, max(8, 0.5 * len(names))), 4.5), draw)


def _networth(entries, start: date, end: date, currency: str) -> Plan:
    days = networth_dates(start, end)
    points = [report.net_worth(entries, day, currency) for day in days]
    unpriced = sorted({name for _, names in points for name in names})
    notes = []
    if unpriced:
        notes.append(f"net worth leaves out currencies without a price: {', '.join(unpriced)}")

    def draw(ax):
        import matplotlib.dates as mdates

        values = [float(value) for value, _ in points]
        ax.plot(days, values, color=BLUE, linewidth=2, marker="o", markersize=6,
                markeredgecolor=SURFACE, markeredgewidth=1.5)
        if len(days) <= 8:
            ax.set_xticks(days)
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
        else:
            locator = mdates.AutoDateLocator()
            ax.xaxis.set_major_locator(locator)
            ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        ax.margins(x=0.08, y=0.2)
        # The ends are labeled, the rest read off the axis. A label goes on the side of its point
        # that the line is not on.
        for index, neighbour in ((0, 1), (len(days) - 1, len(days) - 2)):
            above = values[neighbour] <= values[index]
            ax.annotate(_amount(points[index][0]), (days[index], values[index]),
                        textcoords="offset points", xytext=(0, 9 if above else -16), ha="center",
                        color=MUTED, fontsize=9)
        ax.set_ylabel(currency)
        ax.yaxis.grid(True)

    return Plan([], (8, 4.5), draw, notes)


_PLANS = {"expenses": _expenses, "trend": _trend, "networth": _networth}


def _style(font: str | None) -> dict:
    rc = {
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "text.color": INK, "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
        "axes.edgecolor": GRID, "axes.spines.top": False, "axes.spines.right": False,
        "axes.axisbelow": True, "grid.color": GRID, "grid.linewidth": 0.8,
        "legend.frameon": False,
    }
    if font:
        # The CJK font first, then matplotlib's own for what it lacks; its minus sign (U+2212)
        # is missing from many CJK fonts, so use the hyphen.
        rc.update({"font.family": [font, "DejaVu Sans"], "axes.unicode_minus": False})
    return rc


def chart(
    kind: str, main: Path, start: date, end: date, out: Path | None = None,
    title: str | None = None,
) -> dict:
    """Draw a chart of the days from `start` up to `end`. Returns {"path", "warnings"}.

    `out` is the PNG file to write; without it the chart goes to a new file in the temp folder.
    A `title` that no installed font can draw is replaced by the English default, and the warning
    about the missing font is returned. Account names that cannot be drawn are drawn with
    whatever matplotlib has, and the same warning is returned.
    """
    if kind not in KINDS:
        raise LedgerError(f"unknown chart kind {kind!r}; use {', '.join(KINDS)}")
    if not start < end:
        raise LedgerError(f"the start ({start}) must be before the end ({end})")
    entries, currency = report.load_ledger(main)
    plan = _PLANS[kind](entries, start, end, currency)
    font, font_warnings = pick_font(plan.labels + ([title] if title else []))
    shown = chart_title(kind, start, end, title, font)
    plt = _pyplot()
    image = io.BytesIO()
    with plt.rc_context(_style(font)), warnings.catch_warnings():
        # Without a font the labels come out as boxes; the caller has been told (font_warnings).
        warnings.filterwarnings("ignore", message=r"Glyph \d+ .* missing from")
        fig, ax = plt.subplots(figsize=plan.size, layout="constrained")
        try:
            plan.draw(ax)
            ax.set_title(shown, loc="left", fontsize=13)
            fig.savefig(image, format="png", dpi=DPI)
        finally:
            plt.close(fig)
    try:
        if out is None:
            handle, name = tempfile.mkstemp(
                prefix=f"ledger-{kind}-", suffix=".png", dir=tempfile.gettempdir())
            os.close(handle)
            path = Path(name)
        else:
            path = Path(out)
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(image.getvalue())
    except OSError as err:
        raise LedgerError(f"cannot write the chart: {err}") from err
    return {"path": str(path), "warnings": [*font_warnings, *plan.notes]}

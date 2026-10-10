# Charts

```
uv run apps/ledger/scripts/lk.py chart expenses --ledger-dir ledger --start 2026-09-01 --end 2026-10-01 --title "2026年9月支出"
```

- Kinds: `expenses` (bars by category), `trend` (income and expenses per
  month), `networth` (a line over the months).
- `--start` is the first day, `--end` the day after the last one. For a trend
  or net worth over a year: `--start 2025-10-01 --end 2026-10-01`.
- `--title` sets a title in the user's language (default: one in English).
  `--out <file>.png` picks the file; by default it is a new file in the temp
  folder.

It prints `{"path": "<png>", "warnings": [...]}`. Send the image, then delete
the file:

```
cc-connect send --image "<path>"
rm "<path>"
```

- A warning that no CJK font was found means Chinese text came out as boxes
  and a Chinese title was replaced by the English one. Pass it on to the user
  once, with the fix: a CJK font, which needs root to install, so give them
  the command to run themselves, such as `sudo apt install fonts-noto-cjk` on
  Debian or Ubuntu (another system has its own Noto CJK package). Never run
  `sudo` yourself. Log it with `cc-hub memory log` so later sessions do not
  repeat it. Once they have installed it, delete the chart library's font list
  (`rm -f ~/.cache/matplotlib/fontlist-*.json` on Linux) so the next chart
  finds the new font.
- A warning that net worth leaves out currencies without a price: say which
  holdings are missing from the line.
- `lk: nothing to chart: ...`: the range has no data; say so.

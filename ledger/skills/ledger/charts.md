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
  once, with the fix (installing a CJK font such as `fonts-noto-cjk`, with
  their approval), and log it with `cc-hub memory log` so later sessions do not
  repeat it.
- A warning that net worth leaves out currencies without a price: say which
  holdings are missing from the line.
- `lk: nothing to chart: ...`: the range has no data; say so.

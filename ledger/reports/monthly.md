---
# No command: the agent writes this section from the body below.
---
Bookkeeping reminder (ledger skill). If `start` under `[ledger]` in
`ledger/config.toml` is empty, the books are not set up yet: write nothing.
Otherwise run `uv run apps/ledger/scripts/lk.py summary --ledger-dir ledger`
and write a reminder to export last month's bills from each source listed
under `[import] sources` in `ledger/config.toml`, each with a one-line pointer
to its export steps from its guide (`apps/ledger/skills/ledger/sources/<source>.md`,
or `ledger/importers/<source>.md` for a source the user added); then any money
still owed to the user and by the user, from the `receivables` and `payables`
of the summary. Do not write a monthly summary: it comes after the import. If
no source is listed and nothing is owed, write nothing.

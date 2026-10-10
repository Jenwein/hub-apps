---
schedule: "0 9 1 * *"
default: true
session: new_per_run
---
Monthly bookkeeping reminder (ledger skill). Run
uv run apps/ledger/scripts/lk.py summary --ledger-dir ledger
and send the user, in their language: a reminder to export last month bills
from each source listed under [import] sources in ledger/config.toml, each
with a one-line pointer to its export steps from the source guides; then any
money still owed to them and by them. Do not send a monthly summary yet: it
comes after the import.

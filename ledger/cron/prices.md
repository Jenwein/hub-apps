---
schedule: "0 18 * * 1-5"
default: true
session: new_per_run
---
Update investment prices as described in the ledger skill (investments.md).
If ledger/accounts.beancount declares no commodity with price metadata, do
nothing. Otherwise run bean-price on ledger/main.beancount, append the new
prices to ledger/prices.beancount, run bean-check, and commit only that file.
If bean-check reports an error in prices.beancount, undo your change. Reply
with one line: how many prices were added, or what failed.

---
schedule: "0 18 * * 1-5"
default: true
session: new_per_run
---
Update investment prices as described in the ledger skill (investments.md).
If ledger/accounts.beancount declares no commodity with price metadata, reply
with exactly NO_REPLY. Otherwise run bean-price on ledger/main.beancount,
append the new prices to ledger/prices.beancount and run bean-check. If
bean-check reports an error in prices.beancount, undo your change. If the file
changed, commit only that file. When all of this worked, reply with exactly
NO_REPLY, whether or not prices were added. When something failed (a price
bean-price could not fetch, bean-check, the commit), reply with one line
saying what failed.

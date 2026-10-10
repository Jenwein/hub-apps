# Record from chat

## An entry

1. Work out from what the user said:
   - the date: today, unless they named a day;
   - payee and narration: who was paid and what for, short, in the user's
     words (leave the payee out when they did not name one:
     `2026-10-10 * "lunch"`);
   - the category: an account from `ledger/accounts.beancount`. The defaults
     are `Expenses:` Food, Groceries, Transport, Shopping, Housing, Utilities,
     Communication, Health, Entertainment, Travel, Education, Gifts,
     Insurance, Fees, Other, and `Income:` Salary, Bonus, Interest,
     Investment, Gifts, Other. If none fits, ask;
   - the funding account: the card or wallet they named (a card by its last
     digits); else, for a channel they named ("paid with WeChat"), its account
     under `[chat.channels]` in `ledger/config.toml`; else `[chat] default`.
     If that gives no account, ask;
   - the amount, in the ledger's currency (`[ledger] currency`) unless they
     named another.
2. When the user named a payment channel, add `channel: "<source>"`: `wechat`,
   `alipay`, `meituan`, `jd`. When that bill is imported, the entry gets the
   account that really paid.
3. Write it, every posting with its amount:

   ```
   uv run apps/ledger/scripts/lk.py add --ledger-dir ledger <<'EOF'
   2026-10-10 * "Luckin Coffee" "coffee"
     channel: "wechat"
     Expenses:Food             15.00 CNY
     Assets:WeChat:Change     -15.00 CNY
   EOF
   ```

   `lk add` reads beancount entries on stdin, adds `via: "chat"` to each
   transaction, appends them to the year's file and prints
   `{"written": <n>, "files": [...]}`. It refuses text it cannot parse and
   undoes a write that breaks the ledger check; read the `lk:` message, fix
   the entry, and run it again. If the message is
   `Balance failed for '<account>'`, the entry falls before an earlier
   balance check of that account: never change an amount to satisfy it;
   settle it with the user as in [import.md](import.md), "When a balance check
   fails".

   `via: "chat"` marks a payment the user told you about, which a bill row may
   fix later. An entry that is not one (Yu'E Bao or LingQianTong interest, a
   refund you book yourself, a balance correction) is written with
   `lk add --via ledger --ledger-dir ledger`, so that matching never takes it
   for a chat entry.
4. Reply with the entry as written, then commit (SKILL.md, "Writing to the
   ledger").

Record in chat what no bill will show: cash, Douyin 月付 and other sources the
user does not import. A bill cannot tell a loan or a reimbursement from a
purchase, so record those in chat too, even when a bill will show the payment
later; a payment in the past may already be in the ledger ("Loans, debts,
reimbursements" below).

## Corrections

"Make that 45": find the entry (usually the one you just wrote; else search
`ledger/txns/` for the date and payee), edit it in place, run
`bean-check ledger/main.beancount`, reply with the entry as it is now, and
commit. If bean-check reports `Balance failed`, undo the edit and settle it
with the user as in [import.md](import.md), "When a balance check fails".
Keep an entry's `orderId`, `via` and `bill` metadata as they are: they stop
the next import from booking the payment again.

To delete an entry, remove its lines, then run bean-check, reply and commit as
above. An entry that came from a bill (it has an `orderId`, or its `via` is a
bill source such as `alipay` or `ccb`) comes back the next time that bill is
imported. To drop a bill row for good, tell the user so and keep a note in its
place, a comment with its date, amount and `orderId`:
`; dropped 2026-09-03 滴滴 20.00 CNY, orderId 2026090322001: paid by a friend`.
When an import brings that row back, take it out of the staged file before you
match. If rows like it should never be imported, add an `ignore` rule for them
instead ([import.md](import.md), Rules).

## Loans, debts, reimbursements

| The user says | Postings (the funding account is the card or wallet) |
|---|---|
| "lent Zhang San 500" | `Assets:Receivables:Loans:ZhangSan 500.00 CNY`, funding `-500.00 CNY` |
| "Zhang San paid back 200" | funding `200.00 CNY`, `Assets:Receivables:Loans:ZhangSan -200.00 CNY` |
| "borrowed 1000 from Li Si" | funding `1000.00 CNY`, `Liabilities:Payables:LiSi -1000.00 CNY` |
| "paid Li Si back" | `Liabilities:Payables:LiSi 1000.00 CNY`, funding `-1000.00 CNY` |
| "the 80 taxi is to be reimbursed" | `Assets:Receivables:Reimbursement 80.00 CNY`, funding `-80.00 CNY`, narration says what it was |
| "the company paid the reimbursement" | funding `80.00 CNY`, `Assets:Receivables:Reimbursement -80.00 CNY` |

A payment dated in the past may already be in the ledger: its bill was
imported, or the user told you about it then ("the 80 taxi last Thursday is to
be reimbursed" after the month's bills are in). Look for it first, by its date
(give or take a day or two) and amount:

```
bean-query ledger/main.beancount "SELECT date, payee, narration, account, position, filename, lineno WHERE date >= 2026-10-01 AND date <= 2026-10-03 AND (number = 80 OR number = -80)"
```

If it is there, do not add an entry: edit that one in place (Corrections
above). Change its category, the `Expenses:` or `Income:` posting, to the
receivable or payable of the table; keep its date, amount, funding account and
metadata. If you are not sure it is the same payment, ask. Add a new entry
only when there is no such entry.

A reimbursement that is paid only in part: ask the user whether the rest is
still to come or is their own expense.

## Accounts

- Account components are ASCII, pinyin or English, starting with a capital
  letter; a card's last digits may be the last one:
  `Assets:Receivables:Loans:ZhangSan`, `Assets:Bank:CMB:1234`,
  `Liabilities:CreditCard:CMB:5678`, `Expenses:Pets`.
- Put a person's real name in `name:` metadata on the `open` directive.
- Before using an account that is not open, add its `open` directive to
  `ledger/accounts.beancount`, dated the books' start (`[ledger] start` in
  `ledger/config.toml`), then run `bean-check ledger/main.beancount`:

  ```
  2026-01-01 open Assets:Receivables:Loans:ZhangSan
    name: "张三"
  ```

- Prefer the categories that exist; ask before adding a new one.
- Never open `Expenses:Unsorted`, `Income:Unsorted` or `Assets:Unmapped`.

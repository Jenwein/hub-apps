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
   the entry, and run it again.
4. Reply with the entry as written, then commit (SKILL.md, "Writing to the
   ledger").

Record in chat what no bill will show: cash, Douyin 月付 and other sources the
user does not import. A bill cannot tell a loan or a reimbursement from a
purchase, so record those in chat too, even when a bill will show the payment.

## Corrections

"Make that 45": find the entry (usually the one you just wrote; else search
`ledger/txns/` for the date and payee), edit it in place, run
`bean-check ledger/main.beancount`, reply with the entry as it is now, and
commit. To delete an entry, remove its lines. Keep its `orderId`, `via` and
`bill` metadata as they are: they stop the next import from booking the
payment again.

## Loans, debts, reimbursements

| The user says | Postings (the funding account is the card or wallet) |
|---|---|
| "lent Zhang San 500" | `Assets:Receivables:Loans:ZhangSan 500.00 CNY`, funding `-500.00 CNY` |
| "Zhang San paid back 200" | funding `200.00 CNY`, `Assets:Receivables:Loans:ZhangSan -200.00 CNY` |
| "borrowed 1000 from Li Si" | funding `1000.00 CNY`, `Liabilities:Payables:LiSi -1000.00 CNY` |
| "paid Li Si back" | `Liabilities:Payables:LiSi 1000.00 CNY`, funding `-1000.00 CNY` |
| "the 80 taxi is to be reimbursed" | `Assets:Receivables:Reimbursement 80.00 CNY`, funding `-80.00 CNY`, narration says what it was |
| "the company paid the reimbursement" | funding `80.00 CNY`, `Assets:Receivables:Reimbursement -80.00 CNY` |

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

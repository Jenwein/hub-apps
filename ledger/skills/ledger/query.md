# Answer questions

Answer from the ledger, never from memory. Give the numbers and the period
they cover ("September 1–30", "as of today"). If bills for that period are not
imported yet, say so.

Signs: an expense is positive; income is negative in beancount (flip it for
the user); a liability is negative when the user owes money.

## bean-query

```
bean-query ledger/main.beancount "<query>"
```

Spending by category in a month:

```
bean-query ledger/main.beancount "SELECT root(account, 2) AS category, sum(position) AS total WHERE account ~ '^Expenses:' AND date >= 2026-09-01 AND date < 2026-10-01 GROUP BY category ORDER BY total DESC"
```

The balance of an account (add `AND date < 2026-10-01` for the balance before
a day):

```
bean-query ledger/main.beancount "SELECT sum(position) AS balance WHERE account = 'Liabilities:CreditCard:CMB:1234'"
```

Entries for a payee:

```
bean-query ledger/main.beancount "SELECT date, payee, narration, account, position WHERE payee ~ '滴滴' AND account ~ '^Expenses:' ORDER BY date"
```

## lk summary

```
uv run apps/ledger/scripts/lk.py summary --ledger-dir ledger --month 2026-09
```

Amounts are strings with two decimals, in `currency`:

- `expenses` and `income`: totals per account two levels deep
  (`Expenses:Food`), largest first; `expenses_total`, `income_total`. Income is
  positive here.
- `previous`: `expenses_total` and `income_total` of the month before.
- `net_worth`: `start` (before the month's first day) and `end` (before the
  next month's first day); `unpriced` lists currencies left out because they
  have no price.
- `receivables` (what others owe the user) and `payables` (what the user owes,
  negative), as of today. An amount in another currency reads `"100.00 USD"`.
- `unsorted`: entries in the ledger that still hold a placeholder account.

Without `--month` it prints only `currency`, `receivables` and `payables`: use
it for "who owes me money" and "which reimbursements are still out"
(`Assets:Receivables:Reimbursement`).

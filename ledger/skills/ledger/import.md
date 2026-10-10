# Import bills

The user sends bills exported from Alipay, WeChat, Meituan, JD and their
banks. Each one goes: receive → stage → match → settle → apply. Taobao,
Pinduoduo and Douyin purchases are paid through Alipay or WeChat and show up in
those bills: they need no import of their own.

Import platform bills (Alipay, WeChat, Meituan, JD) before bank statements: a
bank line paid through a platform is then recognised as the same payment. When
several bills arrive at once, take the platform bills first, one bill at a
time.

An inbox folder with a `staged.beancount` and no applied plan (no `plan.json`,
or one with `"applied": false`) is an import that was not finished. Pick it up
when the user comes back to it: match again first, since the ledger may have
changed.

## 1. Receive

- Make a folder `ledger/inbox/<YYYYMMDD-HHMMSS>-<source>/` (the time now,
  for example `ledger/inbox/20261001-093000-alipay/`) and copy the bill into
  it. The inbox is not in git.
- A zip with a password: suggest the user unzip it and send the file inside.
  If they give you the password instead, use it for this run only
  (`unzip -P '<password>' <zip> -d <folder>`, or
  `7z x -p'<password>' -o<folder> <zip>`) and never write it anywhere.
- Tell the source from the file, then read its guide in `sources/<source>.md`.
  A source the user added in `[sources.<name>]` of `ledger/config.toml` has its
  guide in `ledger/importers/<name>.md`.

## 2. Stage

With a converter (alipay, wechat, meituan, jd, ccb and icbc debit cards):

```
uv run apps/ledger/scripts/lk.py convert --ledger-dir ledger --source alipay \
  --file "ledger/inbox/20261001-093000-alipay/<bill file>" \
  --out ledger/inbox/20261001-093000-alipay/staged.beancount
```

A bank statement also needs `--account` with the card's account, for example
`--account Assets:Bank:CCB:1234`: read the card's last digits from the
statement, or ask. It prints `{"staged", "entries", "balance"}`; `balance` is
the closing `balance` directive taken from the statement's running balance, or
null when the statement does not show it clearly.

Without a converter (bosc, nbcb, credit card statements, a bank the user
added):

1. `uv run apps/ledger/scripts/lk.py table "<bill file>"` prints the rows as
   CSV. `--sheet N` picks another sheet of a workbook (from 0);
   `--password '<password>'` opens an encrypted PDF, for this read only.
2. Write the entries yourself to `staged.beancount` in the inbox folder: one
   per row, every posting with its amount, `via: "<source>"` on each, and the
   statement's transaction number, if it has one, as `orderId`. Use
   `Expenses:Unsorted` or `Income:Unsorted` where you do not know the
   category. If the statement shows its closing balance, add a `balance`
   directive dated the day after the last row.

   ```
   2026-09-03 * "滴滴出行" "快车"
     via: "nbcb"
     orderId: "20260903000123"
     Expenses:Transport          20.00 CNY
     Assets:Bank:NBCB:5678      -20.00 CNY

   2026-10-01 balance Assets:Bank:NBCB:5678   1234.56 CNY
   ```

3. Check the count, the totals out and in, and the date range against the
   statement's own totals. Match only when they agree. The user confirms
   these entries before they are applied (step 6, "Confirm first").

## 3. Match

```
uv run apps/ledger/scripts/lk.py match --ledger-dir ledger \
  --staged ledger/inbox/<folder>/staged.beancount --out ledger/inbox/<folder>/plan.json
```

It writes the plan and prints
`{"plan", "new", "duplicate", "fix", "uncertain", "flagged": {"unsorted", "unmapped", "investment"}, "ledger_errors"}`.

- `ledger_errors` is not 0: the ledger already had errors before this
  import. Tell the user, run `bean-check ledger/main.beancount`, fix them
  (SKILL.md, "Writing to the ledger"), then match again before applying.
- Read the plan's `items`. Each has `id` (`s1`, `s2`, … in date order),
  `kind`, `reason`, `date`, `text` (the staged entry), `flags`, `existing` (the
  ledger entries it was compared with: `file`, `line`, `text`) and `fix`.

| kind | meaning | apply |
|---|---|---|
| `new` | not in the ledger | writes it |
| `duplicate` | already in the ledger; reason `order-id` (same order), `statement-row` (this statement line was imported before), `channel` (a bank line paid through a platform whose entry is in), `transfer` (the other side of a transfer already booked) | skips it |
| `fix` | the bill's copy of a chat entry: one recorded from chat, or added in Fava or by hand, that no bill or statement line has been matched to yet (it has no `orderId` and no `bill`). Only when the two are each other's only candidate (near in date, no other such open chat entry has the same or a near amount for the row, and no other row of the batch for the entry) and surely the same payment: the row shows the account that paid, not `Assets:Unmapped`; the same amount; the entry's `channel` is this bill's source, or the row is a line of that account's own bank statement that names no payment channel (a 支付宝 or 财付通 line is the echo of that platform's row); both are transfers between the user's accounts, or both are spending or income; no other category. Else the row is uncertain. A row paid by a method with no rule yet (`Assets:Unmapped`) also keeps its `unmapped` flag: add the `methodAccount` rule (step 4), convert and match again; then it can fix the entry | keeps the chat entry, sets its account to the one that paid, adds the bill's order id and `bill` |
| `uncertain` | needs the user | waits for `--decide` |

A `duplicate` with reason `before-start` is a row dated before `[ledger] start`:
it is not imported, and the report counts it as skipped, before the start of
the books.

## 4. Settle the flags

The flag counts cover new and uncertain items. `apply` refuses to write an item
that is `unsorted` or `unmapped`.

- `unsorted`: no category (`Expenses:Unsorted`, `Income:Unsorted`). Ask the
  user what it was and add a rule that maps it.
- `unmapped`: `Assets:Unmapped` stands for an account the converter could not
  tell.
  - A payment: show the `method:` text and ask which account it is; add a
    `methodAccount` rule. Cards go by their last digits: `method: "(1234)"`.
  - A receipt (money in) comes as `<account> +X` / `Assets:Unmapped -X`. Ask
    what the money is and add a `targetAccount` rule with its account: an
    income (`Income:Salary`), a loan paid back
    (`Assets:Receivables:Loans:<Name>`), a reimbursement
    (`Assets:Receivables:Reimbursement`), or another account of the user's.
    A refund goes against the original expense (Refunds below).
- `investment`: the row mentions a fund, shares or wealth management. Ask; a
  purchase or sale is a holding, not spending ([investments.md](investments.md)).

Transfers between the user's own accounts are not spending: top-ups,
withdrawals, moves between 余额 and 余额宝 or 零钱 and 零钱通, and repayments of
花呗, 白条, 美团月付 or a credit card. Map them with a `targetAccount` rule to
the other account.

After a rule change, convert and match again. Then read the staged entries
the rule was meant to change, and look for others it changed too (Rules,
below). Money must leave the account it left: if a transfer comes out with the
signs the wrong way round, swap `targetAccount` and `methodAccount` in the
rule.

A one-off (a refund, a single transfer) needs no rule: edit that entry in the
staged file and match again. Entries you staged yourself are always settled
this way. Change only the placeholder account; keep the date, the amounts and
the metadata. An investment row is rewritten as a holding
([investments.md](investments.md)). Convert overwrites the staged file, so make
rule changes first and hand edits last.

### Rules

`ledger/importers/<source>.yaml` holds the user's rules for one source. The
app's defaults (`apps/ledger/importers/<source>.yaml`) run before them.

```yaml
rules:
  - method: "(1234)"                 # the card ending 1234
    methodAccount: Liabilities:CreditCard:CMB:1234
  - peer: 滴滴
    targetAccount: Expenses:Transport
  - peer: 某某科技有限公司
    type: 收入
    targetAccount: Income:Salary
  - peer: 美团,饿了么
    sep: ","
    targetAccount: Expenses:Food
  - method: 微信                     # in meituan.yaml: paid through WeChat Pay,
    ignore: true                     # booked from the WeChat bill instead
```

- What a rule matches: `peer` (the payee), `item` (the narration or goods),
  `category`, `type` (收入, 支出, …), `txType`, `method`, `time`
  (`"11:00-14:00"`), `minPrice` and `maxPrice`. Every field given must match.
  Text matches as a substring; `fullMatch: true` makes it exact; `sep` splits
  one field into several alternatives. The row's own values are in the staged
  entry's metadata (`method`, `category`, `type`, `txType`).
- What it sets: `targetAccount` (the category, or the other account of a
  receipt or transfer), `methodAccount` (the account that paid),
  `commissionAccount` (a fee), `ignore: true` (drop the row).
- The last matching rule wins, field by field: put specific rules after
  general ones. The first matching `ignore` drops the row.
- A rule can match more than you meant: a field that a source does not read
  matches every row (ICBC has no `item`), and a short `peer` can match the
  user's own name on a repayment.

## 5. Uncertain items

Show the user each one in a line or two: the staged entry and the `existing`
entries. Then decide by `reason`:

| reason | what it is | what to do |
|---|---|---|
| `possible-duplicate` | a ledger entry near the date shares an account and amount | ask if it is the same payment: same → `skip`, else `new`. If that entry is a chat entry no bill has been matched to (its `via` is `chat` or missing, and it has no `orderId` and no `bill`), do as for `possible-chat` |
| `channel-unmatched` | a bank line paid through Alipay, WeChat, Meituan or JD, with no entry from that platform's bill | usually: import that platform's bill for the period first, then match this statement again. If the user does not export that bill, `new` (it needs a category first) |
| `possible-chat` | a chat entry with the same or a near amount, not surely the same payment (see `fix`); `existing` lists the nearest ones | ask which one, if any, it is. If it is one of them, edit that chat entry as a fix would: the bill's amount and the account that paid, and the row's `orderId` (if it has one) plus `bill: "<source>"`, keeping its category ([record.md](record.md), Corrections). Match again: a row with an order id comes back as a `duplicate`; otherwise it is still uncertain: `skip`. Else `new` |
| `order-id-mismatch` | the same order id with another amount, in the ledger or (when `existing` is empty) earlier in this batch | usually a refund: ask. A refund → `new`, booked against the original expense (Refunds below) |

## 6. Apply

Apply right after matching, on the plan of that same staged file. Never keep
two plans for one staged file: apply does not match again. After any change to
the staged file, the rules or the ledger, match again and use the new plan; its
ids may have changed, so carry your answers over by entry, not by id.

**Confirm first.** Wait for the user's yes before applying when:

- you wrote the staged entries yourself (the `lk table` path): the amounts are
  yours, not a converter's;
- this is the first import from this source: its name is not among the
  sources the ledger already has, listed by
  `bean-query ledger/main.beancount "SELECT DISTINCT entry_meta('via') AS via"`.

Show the user the count, the totals out and in, the date range, and the match's
new, duplicate, fix and uncertain counts, and ask them to check these against
the bill. Apply only after they say yes. If they do not answer in this session,
delete `plan.json`; next time, match again and ask again.

Then apply:

```
uv run apps/ledger/scripts/lk.py apply --ledger-dir ledger \
  --plan ledger/inbox/<folder>/plan.json --decide s4=skip --decide s9=new \
  --document "ledger/inbox/<folder>/<bill file>" --document-account Assets:Alipay:Balance
```

- `--decide <id>=new|skip` for every uncertain item.
- `--document` files the bill as `ledger/documents/<account path>/<date>.<name>`.
  The document account is the card for a bank statement,
  `Assets:Alipay:Balance` for Alipay, `Assets:WeChat:Change` for WeChat, and
  otherwise the account most rows were paid from. It must be open.
- Exit 0 prints `{"written", "skipped", "fixed", "balances", "document"}`.
- Exit 2 writes nothing and prints what is missing:
  - `{"undecided": ["s3", ...]}`: ask the user, then run apply again with a
    `--decide` for each.
  - `{"unresolved": [{"id": "s5", "flags": ["unsorted"]}, ...]}`: items to
    write that still hold a placeholder: settle them (step 4), which ends
    with a new match.
- `lk: ledger changed since match; run match again`: a ledger file changed
  after the match (an entry added in chat or in Fava, another plan applied,
  the `prices` task). Match again, then apply the new plan.
- `lk: plan already applied`: it is in the ledger; do not apply it again. The
  same holds for an error that says the entries are in the ledger.
- `lk: ledger check failed: Balance failed for ...` stops the whole apply.
  If the failing check is the statement's own closing balance, the ledger and
  the statement disagree: look for the missing or extra entry with the user.
  Only if the user agrees to import the rows without that check, delete the
  `balance` line from the staged file and match again, and tell them this
  statement's balance is not checked. If the failing check is one already in
  the ledger, see "When a balance check fails" below.

Then:

1. Delete the inbox folder only once the bill is filed: the apply result's
   `document` is a path. If apply said the document could not be copied, keep
   the folder and tell the user that the bill is still in it, not filed.
2. Commit (`ledger: import <source> <period>`).
3. Tell the user how many new entries, how many duplicates skipped (rows
   dated before the start of the books apart), and what is still pending. List
   each `fix` on a line of its own, chat entry ← bill row, each with its date,
   amount and accounts, so that the user can see they are the same payment:

   ```
   2026-09-06 lunch 35.00 CNY, Expenses:Food / Assets:Bank:CCB:1234 ← 2026-09-06 食堂 午餐 35.00 CNY, paid from Assets:WeChat:Change
   ```

   Ask whether more bills are coming.

## Refunds

Book a refund against the expense it undoes: find the original (same payee or
order id, in the ledger or the batch) and use its account, for example
`Liabilities:CreditCard:CMB:1234 89.84 CNY` / `Expenses:Shopping -89.84 CNY`.
If you cannot find it, ask. The Alipay converter drops a refund together with
its purchase when both are in the bill.

## Balances

- Statements with a running balance: convert stages the closing balance, apply
  writes it, and the ledger check catches any gap from then on.
- Accounts without one: after the user says all bills for the month are in,
  ask for the current balances of their `Assets:Alipay:*` and
  `Assets:WeChat:*` accounts, Huabei, Baitiao, 美团月付 and `Assets:Cash` (a
  screenshot is fine). Compare them with the ledger:

  ```
  bean-query ledger/main.beancount "SELECT account, sum(position) WHERE account ~ '^(Assets:(Alipay|WeChat|Cash)|Liabilities:(Alipay|JD|Meituan))' GROUP BY account"
  ```

  - Yu'E Bao (`Assets:Alipay:YuEBao`) or LingQianTong
    (`Assets:WeChat:LingQianTong`) holding more than the ledger shows: the
    gain is interest. `lk add --via ledger` an entry dated today,
    `<account> X CNY` / `Income:Interest -X CNY`.
  - Any other difference, including less on Yu'E Bao or LingQianTong than
    the ledger shows: look into it with the user before going on. Never book
    negative interest. A correction the user agrees to is written with
    `lk add --via ledger`.
  - Then `lk add` one `balance` directive per account, dated tomorrow. A
    liability's balance is negative: owing 300 on Huabei is
    `<tomorrow> balance Liabilities:Alipay:Huabei  -300.00 CNY`.

### When a balance check fails

A `balance` directive checks the account at the start of its day. An entry
added later and dated before that day (a late bill, a payment the user
remembers afterwards; for a month-end check dated tomorrow, any entry dated
today or earlier) breaks it: `lk add` or `lk apply` fails with
`ledger check failed: Balance failed for '<account>'` and writes nothing.

Never change an amount to satisfy a check. Tell the user the entry falls
before an earlier balance check of that account, and settle it with them:
move the entry's date only if they say the date was wrong; correct the
`balance` directive only if they confirm the real balance was different; if
the entry turns out to be a payment the ledger already has, skip it.

## Month end

When the user says all bills for the month are in:

1. check the balances (above);
2. `uv run apps/ledger/scripts/lk.py summary --ledger-dir ledger --month YYYY-MM`
   ([query.md](query.md) explains the output);
3. draw an `expenses` chart of the month and send it ([charts.md](charts.md));
4. send the summary: spending by category and in total, income, both against
   the previous month, net worth at the start and end, what others owe the user
   and what they owe, and how many entries still need the user.

If more bills for that month come later, import them and send an updated
summary.

## A bank the app does not know

Ask the user what they can export (where in the app, which format), and read a
sample with `lk table`. Write a guide with the same sections as the ones in
`sources/` to `ledger/importers/<name>.md` (files under `apps/` are replaced
when the app updates), add `[sources.<name>]` with `kind = "bank"` to
`ledger/config.toml`, and stage its bills by hand as above. If
double-entry-generator supports that bank, also set `provider = "<provider>"`
there; `lk convert` then works for it.

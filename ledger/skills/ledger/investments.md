# Investments

The user tells you what they bought or sold, how many units and for how much.
Book units at cost; never book a purchase as spending.

## Declare a holding

Each security gets a commodity and an account in `ledger/accounts.beancount`,
dated the books' start:

```
2026-01-01 commodity MAOTAI
  name: "贵州茅台"
  price: "CNY:yahoo/600519.SS"
2026-01-01 open Assets:Invest:Stock:MAOTAI  MAOTAI  "FIFO"
```

- A commodity name is capital letters, digits and `.` `_` `-`, starting with a
  letter, at most 24 characters: `MAOTAI`, `F000569` for fund 000569.
- `price:` tells bean-price where to look: `<currency>:<source>/<symbol>`.
  Listed shares: `yahoo` (`600519.SS` Shanghai, `000001.SZ` Shenzhen, `AAPL`).
  Some mainland funds: `CNY:eastmoneyfund/<fund code>` (not money-market or
  bond funds). Leave `price:` out when there is no source; then add a price
  the user tells you to `ledger/prices.beancount` by hand
  (`2026-09-30 price F000569 1.2345 CNY`), or the holding is left out of net
  worth.
- `"FIFO"` on the account lets a sale take the oldest units first.

## Buy

```
2026-03-02 * "Buy 贵州茅台"
  Assets:Invest:Stock:MAOTAI      100 MAOTAI {1500.00 CNY}
  Expenses:Fees                  5.00 CNY
  Assets:Bank:CCB:1234      -150005.00 CNY

2026-03-02 * "Buy 鹏华增值宝"
  Assets:Invest:Fund:F000569   810.37 F000569 {1.234004 CNY}
  Assets:Alipay:Balance      -1000.00 CNY
```

`{...}` is the cost of one unit. When a fund's fee is taken from the amount
paid, the cost per unit is the amount paid ÷ the units, with six decimals, so
the entry balances. Write it with `lk add` ([record.md](record.md)).

## Sell

```
2026-06-01 * "Sell 贵州茅台"
  Assets:Invest:Stock:MAOTAI     -100 MAOTAI {} @ 1600.00 CNY
  Expenses:Fees                  5.00 CNY
  Assets:Bank:CCB:1234       159995.00 CNY
  Income:Investment         -10000.00 CNY
```

`{}` takes the oldest units and `@` is the sale price. `Income:Investment` is
the gain, negative (a loss is positive): what the units sold for minus what
they cost, here 100 × (1600.00 − 1500.00). Read the cost of the units held with
`bean-query ledger/main.beancount "SELECT sum(position) WHERE account = 'Assets:Invest:Stock:MAOTAI'"`.
Give it its amount like every other posting: `lk match` refuses a staged entry
with a posting left empty. A dividend:
`Assets:Bank:CCB:1234 120.00 CNY` / `Income:Investment -120.00 CNY`.

## Prices

The `prices` task fetches them on weekdays:

```
bean-price ledger/main.beancount >> ledger/prices.beancount
bean-check ledger/main.beancount
```

bean-price sends only the symbols, never holdings or amounts. It fetches
prices for the commodities the user holds and skips prices that are already
in. A price it could not fetch is reported, and changes nothing else. If
bean-check reports an error in `ledger/prices.beancount`, undo your change to
that file. Commit only `ledger/prices.beancount`.

## In bills

`lk match` flags rows that mention funds, shares or wealth management in the
payee, the narration, or the bill's own `txType`, `type` or `category`
(`investment`; the words are `[import] invest_keywords` in
`ledger/config.toml`). Ask the user what each one is:

- a purchase or sale: ask for the units (or the price per unit), then rewrite
  that entry in the staged file as a buy or sell like the ones above, keeping
  its date, metadata and the money that moved, and match again
  ([import.md](import.md), step 4). Check the direction: the converter may show
  a sale as money going out. If the user already told you about it in chat,
  the bill matches that entry and nothing else is needed.
- money moved to the user's brokerage account: a transfer to that account
  (`Assets:Invest:<Broker>:Cash`), not spending.
- Yu'E Bao and LingQianTong are cash accounts, not holdings; their earnings
  are booked at month end ([import.md](import.md), Balances).

# Alipay (`alipay`)

## Export

In the Alipay app: 我的 → 账单 → ⋯ (top right) → 开具交易流水证明 → 用于个人对账.
Pick the period (last month) and an email address. Alipay emails a zip that
holds a CSV; the zip's password arrives as a message in the Alipay app.

## Convert

`--source alipay` (double-entry-generator provider `alipay`). Pass the CSV as
it is; it is GBK text.

| Method text | Account |
|---|---|
| `余额` | `Assets:Alipay:Balance` (default rule) |
| `余额宝` | `Assets:Alipay:YuEBao` (default rule) |
| `花呗` | `Liabilities:Alipay:Huabei` (default rule) |
| `招商银行信用卡(1234)` | a card: `method: "(1234)"` with its `methodAccount` in `ledger/importers/alipay.yaml` at setup; that whole text with `fullMatch: true` once a bill shows it ([import.md](../import.md), step 4) |

Alipay's own categories (`category:`) for dining, transport, groceries, top-ups
and health map to `Expenses:` accounts by default; add `category`, `peer` or
`item` rules for the rest. Document account: `Assets:Alipay:Balance`.

## Known issues

- The converter drops a refunded purchase together with its refund when both
  are in the bill. A refund whose purchase was in an earlier bill comes in
  `unsorted` or `unmapped`: book it against the original expense.
- A row without a payment method comes with `Assets:Unmapped`: ask which
  account paid.
- 花呗 repayments, top-ups, withdrawals and moves between 余额 and 余额宝 are
  transfers: map them to the other account.
- Taobao and Tmall purchases are here; they need no import of their own.

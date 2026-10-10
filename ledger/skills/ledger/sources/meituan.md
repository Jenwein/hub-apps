# Meituan (`meituan`)

## Export

The Meituan app exports the bill (美团交易账单明细) as a CSV file. The menu path
is not verified: ask the user to find the bill export in the app, and log the
path with `cc-hub memory log` once they have found it.

## Convert

`--source meituan` (provider `mt`). Pass the CSV as it is (UTF-8 with a BOM).
The payee is empty and the order title is the narration, so category rules use
`item` (`item: 米线`).

| Method text | Account |
|---|---|
| `美团月付` | `Liabilities:Meituan:MonthlyPay` |
| `中国银行信用卡(1875)` | a card: `method: "(1875)"` at setup; that whole text with `fullMatch: true` once a bill shows it ([import.md](../import.md), step 4) |

Write these to `ledger/importers/meituan.yaml`. Document account: the account
most rows were paid from.

## Known issues

- Orders paid with `微信支付` or Alipay are in that platform's bill too. When
  the user imports that bill, drop them here with an ignore rule
  (`method: 微信` / `ignore: true`, likewise `支付宝`); otherwise ask which
  account paid.
- A refund comes in as money in on the card with `Assets:Unmapped`: book it
  against the original expense.
- 美团月付 repayments (`【美团月付】主动还款…`) are transfers:
  `item: 主动还款` → `targetAccount: Liabilities:Meituan:MonthlyPay`.

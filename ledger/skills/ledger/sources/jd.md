# JD (`jd`)

## Export

In the JD app: 我的 → 我的钱包 → 账单 → ☰ (top right) → 账单导出. The export is
a CSV file.

## Convert

`--source jd` (provider `jd`).

| Method text | Account |
|---|---|
| `钱包余额` | `Assets:JD:Balance` |
| `白条` | `Liabilities:JD:Baitiao` |
| `中国银行信用卡(1875)` | a card: `method: "(1875)"` |

Write these to `ledger/importers/jd.yaml`. JD's own categories are in
`category:` (`category: 食品酒饮` → `Expenses:Groceries`). Document account: the
account most rows were paid from.

## Known issues

- Amounts must keep their two decimals: never open and re-save the file in
  Excel; use the file as JD sent it.
- A purchase and its refund share one order id. The refund comes as an
  `uncertain` item (`order-id-mismatch`) or as a row of its own: book it
  against the purchase's expense. A partly refunded purchase shows its amount
  as `131.77(已退款89.84)`.
- Orders paid through WeChat Pay (`微信-招商银行信用卡`) are in the WeChat bill
  too: as for Meituan, drop them here with `method: 微信` / `ignore: true` when
  the user imports WeChat bills.
- 白条 repayments are transfers to `Liabilities:JD:Baitiao`.

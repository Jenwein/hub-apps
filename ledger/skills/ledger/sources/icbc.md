# Industrial and Commercial Bank of China (`icbc`)

## Export

- Debit card: in ICBC online banking (app or web), query the card's
  transaction details (明细查询) for the month and download them as CSV.
- Credit card: the monthly statement comes by email. The converter does not
  read it: get it as a PDF or spreadsheet and use `lk table` (import.md,
  "Without a converter"), with `via: "icbc"`.

## Convert

`--source icbc --account Assets:Bank:ICBC:<last four digits>` (provider
`icbc`). The card number is at the top of the file (`卡号: 1234****9876`).
Rules can use `peer` (对方户名), `txType` (摘要, such as `工资`, `自动还款`,
`跨行汇款`) and `type`. ICBC has no `item`: a rule on it matches every row.
Document account: the card's account.

## Known issues

- The rows carry no running balance, so no closing balance is staged. If the
  user can tell you the balance at the end of the statement, add a `balance`
  directive for the day after the last row to the staged file before matching.
- Lines paid through Alipay or WeChat (财付通) are matched to those platforms'
  entries: import the platform bills first.
- 自动还款 (automatic credit card repayment) is a transfer to the credit card's
  account.

# China Construction Bank (`ccb`)

## Export

- Debit card, in the CCB app: 我的 → 银行卡 → 管理 → 明细导出 → Excel. The bank
  emails a zip; its password is shown under 查询导出历史 in the same place.
- Credit card: the monthly statement comes by email. The converter does not
  read it: get it as a PDF or spreadsheet and use `lk table` (import.md,
  "Without a converter"), with `via: "ccb"`.

## Convert

`--source ccb --account Assets:Bank:CCB:<last four digits>` (provider `ccb`).
The file is `.xls`; pass it as it is. The card number at the top (账号) is
partly masked; if its last digits are not shown, ask which card it is.

The payee is 对方户名 (`peer` rules), the narration is 交易地点 (`item` rules).
Rows carry the running balance (账户余额), so convert stages the closing balance
when the last day has a single row. Document account: the card's account.

## Known issues

- Lines paid through Alipay, WeChat (财付通), Meituan or JD are matched to
  those platforms' entries: import the platform bills first.
- With several rows on the statement's last day, no closing balance is
  staged. Read the 账户余额 of that day's latest row (by 交易时间) with
  `lk table`, and add the `balance` directive for the next day to the staged
  file before matching.

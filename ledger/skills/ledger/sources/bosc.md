# Bank of Shanghai (`bosc`)

## Export

The export path is not known. Ask the user what the app or online banking lets
them export (CSV, Excel or PDF) and how; log the path with `cc-hub memory log`
once known.

## Convert

No converter. Read the file with `lk table` and write the entries yourself with
`via: "bosc"` on the card's account, `Assets:Bank:BOSC:<last four digits>` or
`Liabilities:CreditCard:BOSC:<last four digits>` (import.md, "Without a
converter"). Show the user the totals before matching. Document account: the
card's account.

## Known issues

- None known yet. A PDF may be encrypted: `--password` with the password the
  user gives you, for this read only.
- Lines paid through Alipay or WeChat (财付通) are matched to those platforms'
  entries: import the platform bills first, and keep the statement's payee
  text in the payee or narration so the match can see the channel.

# WeChat Pay (`wechat`)

## Export

In WeChat: 我 → 服务 → 钱包 → 账单 → 常见问题 → 下载账单 → 用于个人对账. Pick the
period and an email address. WeChat Pay emails a zip; the zip's password
arrives as a message from 微信支付 in WeChat. The file inside is xlsx or csv.

## Convert

`--source wechat` (provider `wechat`).

| Method text | Account |
|---|---|
| `零钱` | `Assets:WeChat:Change` (default rule) |
| `零钱通` | `Assets:WeChat:LingQianTong` (default rule) |
| `中国银行(1234)` | a card: `method: "(1234)"` with its `methodAccount` in `ledger/importers/wechat.yaml` at setup; that whole text with `fullMatch: true` once a bill shows it ([import.md](../import.md), step 4) |
| `招商银行` | a card shown by its bank's name alone (Known issues) |

WeChat has no categories: map merchants with `peer` rules. Document account:
`Assets:WeChat:Change`.

## Known issues

- One export covers at most three months; a monthly import avoids gaps.
- Some card payments (some Meituan orders, 理财通) show the bank's name alone,
  such as `招商银行`, without the card's digits, so a `(1234)` rule misses
  them. When the user has one card of that bank, map the name to it:

  ```yaml
  - method: 招商银行
    fullMatch: true
    methodAccount: Assets:Bank:CMB:1234
  ```

  `fullMatch` keeps the rule off `招商银行(5678)`, another card of that bank.
  With two cards of the bank the name cannot tell which one paid: add no
  rule; ask about each such row and edit it in the staged file
  ([import.md](../import.md), step 4).
- 零钱提现 (to a card), 转入零钱通 and 零钱通转出 are transfers. A withdrawal
  names the bank without the card's digits and carries its fee as a second
  pair of postings. For a withdrawal the converter's sides are swapped: the
  money leaves `targetAccount`. A rule for it:

  ```yaml
  - txType: 零钱提现
    method: 工商银行
    targetAccount: Assets:WeChat:Change
    methodAccount: Assets:Bank:ICBC:9876
    commissionAccount: Expenses:Fees
  ```

- Red packets and transfers received come in with placeholder accounts: ask
  what they are, and check which way the money went.
- Pinduoduo, Meituan and JD orders paid with WeChat Pay are in this bill too.

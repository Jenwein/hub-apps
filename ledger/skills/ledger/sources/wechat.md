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
| `中国银行(1234)` | a card: add `method: "(1234)"` with its `methodAccount` to `ledger/importers/wechat.yaml` |

WeChat has no categories: map merchants with `peer` rules. Document account:
`Assets:WeChat:Change`.

## Known issues

- One export covers at most three months; a monthly import avoids gaps.
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

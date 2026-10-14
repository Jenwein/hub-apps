# Feishu Mail (`feishu`)

A work mailbox on Feishu, usually on the company's own domain: `account detect`
finds it by the domain's MX records (`mx1.feishu.cn` and so on). Lark, the
international version, is another service and not supported yet.

Servers: IMAP `imap.feishu.cn:993`, SMTP `smtp.feishu.cn:465`, both with SSL.
`account add` fills these in; the page that makes the password lists them too.

## Turn on IMAP/SMTP

Mail clients work only when the company's Feishu admin has allowed them. In
the Feishu admin console the switch is at 产品设置 → 邮箱 → 邮箱管理工具 →
用户功能权限 → 第三方邮箱客户端. It is likely off unless someone turned it on.

The user cannot see or change that switch. They only see whether it is on:
if the entry 第三方邮箱客户端登录 in step 2 of "Password" is missing, the admin
has not allowed mail clients, and the user asks the company's Feishu admin to
turn it on. There is nothing else to try.

## Password

The password for mail clients is made only in the Feishu **desktop** app, not
on the phone and not on the web:

1. Click the profile picture (头像) → 设置.
2. 邮箱 → 第三方邮箱客户端登录 → 进入设置.
3. Choose the device (选设备), then 生成 (generate).

The page then shows the password with the mailbox address and the server
settings (`imap.feishu.cn:993`, `smtp.feishu.cn:465`, SSL). The user sends the
password in the chat. To revoke one later, the user looks in the same place.

## Known issues

- One mail sent through a mail client can be at most 36 MB.
- If the admin turns mail clients off, logins are refused even with the right
  password: the user asks the admin.
- To be confirmed on a real account: whether mail sent through SMTP appears in
  已发送 by itself (the app saves a copy there; if every sent mail shows
  twice, report it), and whether the mailbox has an archive folder (if not,
  the app asks before it creates one).

# QQ Mail (`qq`)

Addresses ending in `@qq.com` (the QQ number or a chosen name) or
`@foxmail.com`. Tencent enterprise mail (exmail, a company's own domain on
Tencent) is another service and not supported yet.

Servers: IMAP `imap.qq.com:993`, SMTP `smtp.qq.com:465`, both with SSL.
`account add` fills these in.

## Turn on IMAP/SMTP

In the web mail (mail.qq.com), signed in, open 设置 and find the service
switches named POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务. Depending on the
version of the web mail they are under 账号 (scroll down), or under 账号与安全
→ 安全设置. Turn on IMAP/SMTP服务. QQ asks for a check with the phone bound to
the account (a text message to send, or a code); follow its prompts.

## Password

Mail clients log in with a 授权码 (authorization code), not the QQ password.
QQ shows it once the service is on; the same place makes a new one later
(生成授权码). Have the user copy it at once and send it in the chat. To revoke a
code later, the user looks on the same settings page.

## Known issues

- A refused login after it worked before: the code was revoked or the
  service was turned off. Have the user check the switches above and make a
  new code.
- QQ has a setting to save mail sent through SMTP on the server. The app also
  saves a copy of each sent mail in Sent; to be confirmed on a real account
  whether a sent mail then shows twice (if it does, report it).
- To be confirmed on a real account: the largest mail QQ takes from a mail
  client (the app assumes 50 MB, the limit for attachments in QQ's help).

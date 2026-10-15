# NetEase Mail: 163, 126, yeah.net (`163`, `126`, `yeah`)

Addresses ending in `@163.com`, `@126.com` or `@yeah.net`. NetEase enterprise
mail (`qiye.163.com`) is another service and not supported yet.

| Provider | IMAP | SMTP |
|---|---|---|
| `163` | `imap.163.com:993` | `smtp.163.com:465` |
| `126` | `imap.126.com:993` | `smtp.126.com:465` |
| `yeah` | `imap.yeah.net:993` | `smtp.yeah.net:465` |

Both with SSL. `account add` fills these in; the user types none of them.

## Turn on IMAP/SMTP

In the web mail (mail.163.com, mail.126.com or mail.yeah.net), signed in:
设置 (top of the page) → POP3/SMTP/IMAP → turn on IMAP/SMTP服务. NetEase asks
for a check with the phone bound to the account (usually a text message to
send or a code to enter); follow its prompts.

## Password

Mail clients log in with a 授权码 (authorization code), not the web password.
Turning on the service ends with a page that shows the code; if the user
skipped it, the same settings page makes a new one (新增授权密码 or similar).
Have them copy it at once and send it in the chat. The codes are managed on
the same settings page; that is also where a code is revoked.

## Known issues

- NetEase refuses mail clients that do not identify themselves after login
  ("Unsafe Login"). The app sends the IMAP `ID` command it wants, so this
  should not happen; if the message still appears, report it with the step.
- A refused login after it worked before: the authorization code was revoked
  or the IMAP/SMTP service was turned off. Have the user check the settings
  page above, turn the service on again if needed, and make a new code.
- To be confirmed on real accounts: the largest mail NetEase takes from a
  mail client (the app assumes 20 MB), and whether mail sent through SMTP
  appears in 已发送 by itself (the app saves a copy there; if every sent mail
  shows twice, report it).

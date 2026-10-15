# Gmail (`gmail`)

Addresses ending in `@gmail.com` or `@googlemail.com`, and a company's own
domain whose mail is on Google Workspace (`account detect` finds it by the
domain's MX records).

Servers: IMAP `imap.gmail.com:993`, SMTP `smtp.gmail.com:465`, both with SSL.
`account add` fills these in.

## Turn on IMAP/SMTP

For a personal Gmail account IMAP is on, with no switch to turn. If the
account's Gmail settings still show one (Settings → See all settings →
Forwarding and POP/IMAP → IMAP access), it must say enabled. For a Google
Workspace account, the company's admin can turn IMAP off for its users: if
the login is refused with a correct app password, the user asks the admin.

## Password

Mail clients log in with an app password, not the Google password. It needs
2-Step Verification on the account:

1. In the Google Account (myaccount.google.com) → Security, turn on 2-Step
   Verification if it is off.
2. Open App passwords (myaccount.google.com/apppasswords), give the app a name
   such as "cc-hub mail", and create it.
3. Google shows a 16-letter password in four groups. The user sends it in the
   chat; pass the 16 letters without the spaces.

If App passwords is missing: 2-Step Verification is off, the account uses
Advanced Protection or only security keys, or (a work or school account) the
admin does not allow app passwords. The same page lists and revokes app
passwords.

## Known issues

- Changing the Google password revokes the app passwords: the next check fails
  to log in. Make a new app password and send it again.
- Gmail saves sent mail in Sent by itself; the app does not add a copy.
- Archive takes mail out of the inbox; it stays in All Mail with its labels.
  Delete moves it to Trash, which Gmail empties after 30 days.
- Folder names follow the account's language; the app finds Trash, Sent and
  All Mail by their IMAP flags, not by name.
- The largest mail Gmail sends is 25 MB.

# Mailboxes and providers

Four kinds of mailbox work: Feishu enterprise mail, QQ mail, Gmail and Outlook. Each needs
something done in the account first, by the user. Walk them through it in their language; you
never see or handle a password.

## Add or fix a mailbox

1. The user prepares the account (the provider's part below).
2. Add a `[[mailbox]]` entry to `mail/mailboxes.toml`: the `address` from the user, a short `name`
   (lowercase letters, digits and dashes, such as `qq` or `work`) that you use in every command,
   and the `provider`. Leave the folders empty for now. Never write a password in this file.
3. The user types the password, or signs in, in the server's terminal, in the hub's folder: for
   Feishu, QQ and Gmail `uv run apps/mail/scripts/mx.py secret-set <name>` (the password is typed
   twice and not shown), for Outlook `uv run apps/mail/scripts/mx.py oauth-login <name>`. Never
   ask them to paste a password or code into the chat. Both commands refuse to run without a
   terminal.
4. `mx configure` writes the generated configs and lists what is still missing:
   `missing_secrets` (no password stored) and `missing_sign_in` (Outlook, not signed in).
5. `mx folders <name>` lists the server's folders (`folders`: `name` as the server spells it,
   `shown` as it reads) and `guess`: the `name` it takes for the `sent`, `trash` and `archive`
   folders, or null. Write them under `[mailbox.folders]` with the `name`, not `shown`. Where a
   guess is null, show the user the `shown` names and write the `name` of the one they pick; a
   role that does not exist stays empty. Then run `mx configure` again.
6. `mx check <name>` must say `{"ok": true}`, and `mx list --mailbox <name> --limit 3` shows the
   three newest mails: show them to the user.
7. Commit: `git add -- mail/mailboxes.toml && git commit -m "mail: add <name>" -- mail/mailboxes.toml`
   (do not push). If `mail/config.toml` changed too, add it to both paths.

```
uv run apps/mail/scripts/mx.py configure
uv run apps/mail/scripts/mx.py folders qq
uv run apps/mail/scripts/mx.py check qq
uv run apps/mail/scripts/mx.py list --mailbox qq --limit 3
```

Other keys of an entry, rarely needed:

| Key | Meaning |
|---|---|
| `display_name` | the sender name on mail the user sends |
| `save_copy` | `true` to put a copy of sent mail in the sent folder, `false` where the server keeps one itself (default: what the provider needs) |
| `sasl` | `"plain"` (default) or `"login"`: how a password is sent. Try `"login"` when the server refuses a password that is right |
| `imap_host`, `imap_port`, `imap_tls`, `smtp_host`, `smtp_port`, `smtp_tls` | a server other than the provider's usual one; `tls` is `"tls"`, `"starttls"` or `"none"` |

## Feishu enterprise mail

Signs in with a client password that Feishu creates for third-party mail programs, not the
account password.

Before: the company's admin allows third-party mail clients (IMAP and SMTP) in the Feishu admin
console; then the user creates a client password in the mail settings of their own account. The
menu names change between Feishu versions, so describe what is needed rather than a path. The
password is shown once, and the user types it into `mx secret-set`.

```toml
[[mailbox]]
name = "work"
address = "you@company.com"
display_name = "Your Name"
provider = "feishu"

[mailbox.folders]
sent = "Sent"
trash = "Trash"
archive = ""
```

The servers are `imap.feishu.cn` and `smtp.feishu.cn`; a company on another address sets
`imap_host` and `smtp_host`. The folder names above are only an example: take them from
`mx folders work`.

## QQ mail

Signs in with an authorization code that QQ creates, not the QQ password.

Before: in QQ Mail on the web, Settings, Account: turn on the IMAP/SMTP service, and create an
authorization code (QQ may ask for a text-message check). The code is shown once, and the user types
it into `mx secret-set`. `mx` makes the client identify itself, as QQ requires.

```toml
[[mailbox]]
name = "qq"
address = "123456789@qq.com"
display_name = "Your Name"
provider = "qq"

[mailbox.folders]
sent = "Sent Messages"
trash = "Deleted Messages"
archive = ""
```

QQ has no archive folder of its own. To use `move --to archive`, the user creates a folder (such
as Archive) in QQ Mail and it goes under `archive`; until then `move --to archive` says that the
mailbox has no archive folder set.

## Gmail

Signs in with an app password, not the Google password.

Before: two-step verification is turned on for the Google account; then the app passwords page of
the Google Account (Security) creates a 16-character app password (the user types it into
`mx secret-set` without spaces). A work account whose admin turned app passwords off cannot create one; then Gmail cannot
be used here.

```toml
[[mailbox]]
name = "gmail"
address = "you@gmail.com"
display_name = "Your Name"
provider = "gmail"

[mailbox.folders]
sent = "[Gmail]/Sent Mail"
trash = "[Gmail]/Trash"
archive = "[Gmail]/All Mail"
```

Gmail keeps a copy of what is sent by itself, so `save_copy` stays at its default (`false`). The
folder names follow the language of the Gmail interface: with another language they read like
`[Gmail]/已发邮件` for the sent mail. Use the `name` that `mx folders gmail` prints, as the server
spells it.

## Outlook

Signs in with Microsoft's OAuth; there is no password. Personal accounts (outlook.com, hotmail.com,
live.com) work. A work or school account may have third-party sign-ins blocked by its admin.

Before: `mx install-tool ortie` (it downloads the pinned ortie from its official release and checks
its SHA-256; ask the user first), then the entry below and `mx configure`. The user then types
`uv run apps/mail/scripts/mx.py oauth-login <name>` in the server's terminal. It prints a web
address and a short code: on any device the user opens the address, enters the code, signs in to
Microsoft and approves. If Microsoft refuses that, the user runs it again with `--paste`: it prints
a sign-in address, the browser ends on a page that does not load (`https://localhost/?code=...`),
and the user pastes that address back into the terminal. They never paste it into the chat.

```toml
[[mailbox]]
name = "outlook"
address = "you@outlook.com"
display_name = "Your Name"
provider = "outlook"

[mailbox.folders]
sent = "Sent"
trash = "Deleted"
archive = "Archive"
```

Outlook names its folders `Sent` or `Sent Items`, `Deleted` or `Deleted Items`, and `Archive`; take
them from `mx folders outlook`. Outlook cannot search message bodies for non-ASCII words.

## Folder names in general

`mx folders` guesses from the usual names: `Sent Messages`, `Sent`, `Sent Items`, `Sent Mail`,
`[Gmail]/Sent Mail` for sent mail; `Deleted Messages`, `Trash`, `Deleted`, `Deleted Items`,
`[Gmail]/Trash` for the trash; `Archive`, `[Gmail]/All Mail` for the archive; and their Chinese
equivalents. A server may spell a name that is not plain ASCII in a coded form (such as `&XfJT0ZAB-` for the Chinese word for "Sent");
write the `name` exactly as `mx folders` prints it, and show the user the `shown` form. `inbox`
stays `INBOX`.

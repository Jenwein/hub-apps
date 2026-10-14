# hub-apps

A [cc-hub](https://github.com/Jenwein/cc-hub) tap: a git repository of apps for
the agent that lives in your hub. cc-hub only copies an app's files into the
hub. The agent installs the software, asks the setup questions and does the
work, and runs a command only after you agree to it.

| App | What it does |
|---|---|
| [`ledger`](ledger/) | Bookkeeping with beancount and Fava: record spending from chat, import Alipay, WeChat and bank bills, ask about your money, get charts. |
| [`mail`](mail/) | Your mailboxes in the chat: important mail pushed within minutes, a daily digest of the rest, search, archive, delete, and replies sent only after you approve the draft. Needs cc-hub 0.5.0. |

## Install

```
cc-hub tap add Jenwein/hub-apps
cc-hub app install hub-apps/ledger
cc-hub app install hub-apps/mail
```

Or ask your agent in the chat: "Install the ledger app from Jenwein/hub-apps."

`cc-hub app install` prints what the app needs (software, scripts, scheduled
tasks) and changes nothing. When you agree, run the confirm command it printed,
or tell your agent to. The agent then installs the software you approve and
asks the setup questions.

| Command | What it does | Example |
|---|---|---|
| `cc-hub tap add <owner/repo>` | Registers a tap. `<owner/repo>` is a GitHub repository. | `cc-hub tap add Jenwein/hub-apps` |
| `cc-hub app search [words]` | Lists the apps of your taps. `[words]` narrows the list. | `cc-hub app search ledger` |
| `cc-hub app install <tap>/<app>` | Shows what an app needs. | `cc-hub app install hub-apps/ledger` |
| `cc-hub app list` | Lists installed apps and whether their setup is done. | `cc-hub app list` |
| `cc-hub app update <app>` | Shows what a new version changes. | `cc-hub app update ledger` |
| `cc-hub app remove <app>` | Shows what removing an app does. | `cc-hub app remove ledger` |

`<name>` is required, `[name]` may be left out. Your agent runs the follow-up
steps, such as the confirm commands.

## ledger

Your books are plain text, a [beancount](https://github.com/beancount/beancount)
ledger in `ledger/` in your hub, kept in the hub's git history. You use them
mostly by chatting with the agent; [Fava](https://github.com/beancount/fava)
shows them as web pages when you want the full reports. The agent talks to you
in your language. Account names are English by default and yours to change.

- **Record from chat.** Say it and the agent writes the entry and shows it
  back.
- **Import bills.** Send the files exported from Alipay, WeChat, Meituan, JD
  and your banks. The agent converts them, skips what is already in the books,
  and asks about what it cannot tell.
- **Ask.** Any question about your money is answered from the ledger.
- **Charts.** The agent draws spending by category, income and spending per
  month, or net worth, and sends the picture to the chat.
- **Covered.** Everyday income and spending, bank cards, the sub-accounts of
  Alipay and WeChat, credit cards and Huabei, loans you gave and took,
  reimbursements, and shares and funds with daily prices.
- **Scheduled tasks**, in the server's local time: `prices` runs on weekdays
  at 18:00 and updates the prices of your investments (it does nothing
  without any); `monthly` runs on the 1st at 09:00 and reminds you to export
  last month's bills, with the money still owed to you and by you.

### Software it installs

The agent shows you each command and what it does, and installs nothing until
you agree.

| Software | What for | How it is installed |
|---|---|---|
| Fava, with beancount, beanquery and beanprice | the ledger checks, queries, price lookups and the web reports | `uv tool install --force fava --with-executables-from beancount,beanquery,beanprice`, one environment that provides `fava`, `bean-check`, `bean-format`, `bean-query` and `bean-price` |
| [double-entry-generator](https://github.com/deb-sig/double-entry-generator) v2.16.0 | turns Alipay, WeChat, Meituan, JD, CCB and ICBC bills into entries | the release from GitHub into `~/.local/bin` (Homebrew on macOS); no root |
| [uv](https://docs.astral.sh/uv/) | installs the above and runs the app's scripts | the official installer, only if it is missing |

The app's own scripts (matching, charts, reading spreadsheets and PDFs) fetch
their Python packages the first time they run, through `uv run`. Fava runs as a
background service of your user (systemd on Linux, launchd on macOS), which
the agent sets up with your approval. cc-connect carries the chat, the
scheduled tasks and the charts. Optional and yours to install: Tailscale (Fava
on your phone) and a CJK font such as `fonts-noto-cjk` (Chinese text in charts).

If your server reaches GitHub and the price sources through a proxy, put the
proxy variables where cc-connect starts the agent, next to `PATH`, in
`[projects.agent.options.env]` of cc-connect's config:

```toml
[projects.agent.options.env]
HTTPS_PROXY = "http://127.0.0.1:7890"
HTTP_PROXY = "http://127.0.0.1:7890"
NO_PROXY = "127.0.0.1,localhost"
```

Use your own proxy address. The app does not set up a proxy itself.

### Using it

After the install, say "set up the ledger app". The agent asks for your main
currency, the day the books start with the balance of each account that day,
your bank cards, the bill sources you export, which account a "paid with
WeChat" entry should use, and whether you want Fava on your phone. It then
starts Fava.

**Day to day, record when you like; nothing is required.** What you pay with
Alipay, WeChat or a bank card is filled in when you import the bills, so not
recording it loses nothing. Recording as you go keeps the books current and
keeps what the money was for.

- What no bill shows has to be said in the chat: cash, and anything you cannot
  export (Douyin Monthly Pay, say).
- What a bill cannot tell is best said in the chat: money lent, an expense
  that will be reimbursed, buying or selling shares or funds.
- A mistake: "make that 45".

```
Lunch 45 with WeChat
Lent Zhang San 500
The 80 taxi is to be reimbursed
Make that 45
How much did I spend on food last month?
Chart this month's spending
```

**Once a month, import the bills.** On the 1st the bot reminds you. Export
last month's bills from each platform and bank and send the files to the chat.
Once a month is easiest, and WeChat exports at most three months at a time.
Bills often come in a zip with a password: unzip it yourself and send the file
inside. If you give the password in the chat, the agent uses it once and does
not write it down. When the import is done the agent tells you how many entries
are new, how many matched what was already there, and how many wait for your
answer; then it checks the balances that bills do not show and sends the
month's summary with a chart.

### Viewing Fava

Fava listens on `127.0.0.1` only, on port 5000 unless you change `[fava] port`
in `ledger/config.toml`. On a computer, open an SSH tunnel and then
`http://localhost:5000`:

```
ssh -L 5000:localhost:5000 you@your-server
```

Replace `you@your-server` with your login and the server's address. On the
server itself, open `http://localhost:5000` directly. On Windows Fava is not
resident; the agent starts it when you ask.

On a phone, the agent uses Tailscale: it runs `tailscale serve --bg 5000` and
tells you the `https://<machine>.<tailnet>.ts.net` address. Only devices in your
tailnet can open it; it is never public. You install Tailscale on the server
(`curl -fsSL https://tailscale.com/install.sh | sh`, then `sudo tailscale up`,
which needs root) and on the phone, logged in to the same account. A domain with
a password in front of Fava is not something the app sets up.

### Supported bill sources

| Source | Export | Read by |
|---|---|---|
| Alipay | CSV in a zip, from the app's transaction statement for personal reconciliation | double-entry-generator |
| WeChat Pay | xlsx or CSV in a zip, from the app's bill download | double-entry-generator |
| Meituan | CSV, from the app's bill export | double-entry-generator |
| JD | CSV, from the app's bill export | double-entry-generator |
| China Construction Bank | debit card `.xls` from the app; credit card statements as PDF or spreadsheet | double-entry-generator for debit cards, otherwise the agent |
| ICBC | debit card CSV from online banking; credit card statements as PDF or spreadsheet | double-entry-generator for debit cards, otherwise the agent |
| Bank of Shanghai | CSV, Excel or PDF | the agent reads the file |
| Bank of Ningbo | CSV, Excel or PDF | the agent reads the file |

Where "the agent reads the file", a script prints the spreadsheet or PDF as
text, the agent writes the entries, and you confirm the totals before anything
is added. Taobao, Tmall, Pinduoduo and Douyin have no bill export of their
own: what you buy there is paid through Alipay or WeChat and appears in those
bills. Each source has a guide with the export steps and the known pitfalls in
`ledger/skills/ledger/sources/`.

### Add a bank

Tell the agent which bank you want to import. It asks what you can export and
how, reads a sample, and writes a guide in the same form as the ones above to
`ledger/importers/<name>.md`, in your ledger and not under `apps/`, because
`apps/` is replaced when the app updates. It also adds the bank to
`ledger/config.toml`:

```toml
[sources.cmb]
kind = "bank"
```

If double-entry-generator supports the bank, the agent adds its
`provider = "<provider>"` there and the bank's bills are converted like the
built-in ones. Otherwise the agent reads each statement itself.

### Your data

The ledger belongs to you. It stays in your hub, the chat and the hub's own git
remote; it is never pasted into a website or another service. Bills are
converted on your machine, the app's scripts make no network calls, and price
lookups send only the symbols of what you hold, not amounts. Removing the app
leaves `ledger/` as it is.

## mail

Your mailboxes, watched by the agent in your hub. Important mail is pushed to
the chat within minutes, the rest comes in a daily digest, and you search,
read, archive, delete and write mail by chatting. Needs cc-hub 0.5.0 or newer,
for its script, on-demand and restricted tasks.

- **Supported mailboxes.** NetEase 163, 126 and yeah.net; QQ Mail (also
  foxmail.com); Gmail (also Google Workspace); Feishu Mail. Not yet: Outlook
  and Hotmail, iCloud, Lark, Microsoft 365, and Tencent or NetEase enterprise
  mail. The app logs in over IMAP and SMTP with an app password or
  authorization code, which you make in the mailbox's settings; the agent
  tells you where. Feishu Mail needs your company's Feishu admin to allow mail
  clients.
- **Pushes.** Every 5 minutes a script checks the inbox of each mailbox,
  read-only: nothing is marked read, moved or deleted. When new mail arrived,
  it wakes the agent, which judges each mail against your rules and pushes
  the important ones in one message. Reply to a push in the chat ("回他：好的，
  周三可以") and the agent drafts the answer. Mail that was in the inbox before
  you added the mailbox is never pushed.
- **Daily digest**, at 08:00 your time: what arrived, counted by mailbox and
  kind, a few mails worth a look, and anything wrong with a mailbox. If the
  background check stops, the digest says so first.
- **Your rules.** One sentence of what counts as important, for all mailboxes
  or one of them ("only mail sent to me directly that needs my answer");
  senders that are never pushed; corrections you give in the chat ("don't
  push these").
- **Nothing happens behind your back.** A mail is sent only after you saw the
  whole draft and said send; mail is deleted only after you saw the list and
  said delete, and it goes to Trash. The tasks that read your mail can run
  only the app's read-only commands (on Claude Code, cc-hub's guard enforces
  that), so a mail that tries to give the agent orders cannot make it send or
  delete anything. Passwords stay in `secrets/mail/` on your machine, never in
  git.
- **Attachments** are described by name and content; the agent sends one to
  the chat when you ask (Feishu takes files up to 30 MB, images up to 10 MB).
  To attach a file over 2 MB that you send from Feishu, you need cc-connect
  1.5.1 or newer.

You can say these in the chat:

```
Add my QQ mailbox 12345@qq.com
回他：好的，周三可以
Find the contract Zhang sent last month
Archive this week's newsletters
Forward the offer mail to hr@example.com
Don't push mail from GitHub any more
Send the digest at 7:30
```

The agent changes the times with these commands; you can run them yourself
too:

| Command | What it does | Example |
|---|---|---|
| `cc-hub cron set mail/poll "<cron>"` | How often mail is checked. `<cron>` is a cron expression (minute, hour, day, month, weekday) in the server's time, in quotes. | `cc-hub cron set mail/poll "*/15 * * * *"` |
| `cc-hub cron set mail/digest "<cron>"` | When the digest comes. | `cc-hub cron set mail/digest "30 7 * * *"` |
| `cc-hub cron set mail/digest --off` | No digest; `--on` brings it back. | `cc-hub cron set mail/digest --off` |
| `cc-hub cron reset <task>` | Back to the app's time. `<task>` is `mail/poll` or `mail/digest`. | `cc-hub cron reset mail/digest` |

Then the agent registers the task anew in cc-connect, in a direct chat.

### Your data

The mailboxes and rules are in `mail/config.toml`, in the hub's git history.
The local index of your mail (for search), the drafts and the delete lists are
in `mail/.state/`, the downloaded attachments in `mail/attachments/`; neither
is in git, so neither moves with the hub to another machine. Mail goes only
between your mail servers, your hub and the chat. Removing the app leaves
`mail/` as it is; the agent asks whether to delete the passwords.

## Develop

```
uv run pytest -q
uv run ruff check .
cc-hub app install /path/to/hub-apps/ledger --link --yes
```

The last command links the working copy into a hub as an app, so edits apply at
once. `/path/to/hub-apps/ledger` is the `ledger/` directory of your clone; use
`mail/` for the mail app. The tests use synthetic bills and mail only; some
fixtures are copied from double-entry-generator's examples (Apache-2.0, see
`tests/fixtures/deb/NOTICE`). No real bill, ledger or mail belongs in this
repository. The mail tests marked `imapserver` need an IMAP and SMTP test
server and run only when `MAIL_TEST_IMAP=host:port` is set.

## License

MIT. See [LICENSE](LICENSE).

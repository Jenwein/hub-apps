# hub-apps

A [cc-hub](https://github.com/Jenwein/cc-hub) tap: a git repository of apps for
the agent that lives in your hub. cc-hub only copies an app's files into the
hub. The agent installs the software, asks the setup questions and does the
work, and runs a command only after you agree to it.

| App | What it does |
|---|---|
| [`ledger`](ledger/) | Bookkeeping with beancount and Fava: record spending from chat, import Alipay, WeChat and bank bills, ask about your money, get charts. |
| [`mail`](mail/) | Your mailboxes from chat: search and read mail, find verification codes, write and send with a confirmation code, daily and monthly report sections, optional alerts for urgent mail. |

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
asks the setup questions. The ledger and mail apps need cc-hub 0.7.0 or later.

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
- **Prices.** A scheduled task runs on weekdays at 18:00, in the server's
  local time, and updates the prices of your investments (it does nothing
  without any). It writes to the chat only when something fails.
- **Monthly report.** The ledger adds a part to the hub's monthly report (the
  1st, 09:00 unless you move it): a reminder to export last month's bills,
  with the money still owed to you and by you. During setup the agent offers
  to turn the monthly report on.

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

**Once a month, import the bills.** On the 1st the monthly report reminds you. Export
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

Your mailboxes from the chat. You talk to the agent in a direct chat, in your language, and it
searches and reads your mail, finds verification codes, writes, replies to and forwards mail, and
can tell you when something urgent arrives. It needs cc-hub 0.7.0 or later.

- **Search and read.** "Mail from GitHub this week", "unread in QQ", "what did I send from QQ
  last week".
  The agent lists mail across your mailboxes, newest first. The original of a mail, or the answer
  to a question about it, goes to your private chat.
- **Verification codes.** "What is the QQ code?" gives the code from mail of the last few minutes.
- **Write.** Write a mail, reply to one in your own words, or forward one. The draft goes to your
  private chat with a one-time confirmation code, and the mail is sent only when you reply with
  that code.
- **Attachments.** The agent lists the attachments of a mail and sends one to the chat. A file you
  send in the chat can be attached to the mail you write.
- **Mark and move.** Mark mail read, or move it to the archive or the trash. Nothing is deleted
  for good.
- **Rules.** Say in your own words how mail is sorted ("GitHub notifications: ignore"; "mail from
  my landlord goes in the daily report").
- **Reports.** The mail app adds a section to the hub's daily report (the mail that arrived,
  grouped by kind) and one to the monthly report (new versions of the tools it uses, how long
  alerts were down, mailboxes that could not sign in). cc-hub runs the section's command itself;
  during setup the agent shows you the command and asks you to approve it.
- **Alerts (optional).** A background service on the machine that runs cc-connect keeps a
  connection to each inbox and sends one chat message per urgent mail, with a warning when the
  mail looks like phishing. Alerts are off until you turn them on.

### Mailboxes

Four kinds of mailbox work. Each needs something done in the account first; the agent walks you
through it.

| Mailbox | What it needs first |
|---|---|
| Feishu enterprise mail | Your company's admin allows third-party mail clients; you create a client password in your mail settings. |
| QQ mail | The IMAP/SMTP service turned on and an authorization code created in QQ Mail's settings. |
| Gmail | Two-step verification on, and an app password from your Google Account. |
| Outlook (outlook.com, hotmail.com, live.com) | Nothing in the account: you sign in to Microsoft once, with a code you enter on any device. |

### Software it uses

The agent shows you each command and what it does, and installs nothing until you agree.

| Software | What for | How it is installed |
|---|---|---|
| [himalaya](https://github.com/pimalaya/himalaya) v2.2.1 | reads and sends the mail over IMAP and SMTP | the app's own downloader, from the official GitHub release, SHA-256 checked, into `~/.local/bin`; no root |
| Claude Code | the mail reader, which has no tools; needed even when the hub's agent is Codex | the official installer; you sign in once in the server terminal (`claude`, then `/login`) |
| [uv](https://docs.astral.sh/uv/) | runs the app's scripts | the official installer, only if it is missing |
| [carillon](https://github.com/pimalaya/carillon) v0.2.0 | alerts only: watches the inboxes | the app's downloader, when you turn alerts on |
| [ortie](https://github.com/pimalaya/ortie) v2.3.0 | Outlook only: the Microsoft sign-in | the app's downloader, when you add an Outlook mailbox |

The app's scripts fetch their Python packages the first time they run, through `uv run`. Alerts run
as a background service of your user: systemd on Linux and WSL (which needs systemd turned on),
launchd on macOS, Task Scheduler on Windows.

### Using it

After the install, say "set up the mail app". The agent checks the mail reader, records your
private chat (a test message arrives there), and asks which mailboxes you use. For each one you type
its password in the server terminal, never in the chat. Then it asks whether you want alerts.

```
Any new mail today?
What is the verification code from QQ?
Mail from GitHub this week
Reply to the second one: yes, Thursday at 3 pm works for me
Write to bob@example.org about the refund
Move that one to the archive
```

### Safety

Mail is read by a reader without tools (Claude Code with every tool switched off): the agent never
reads a mail body or an attachment, and treats the subjects, sender names and attachment names it
sees in lists as data, never as instructions. What the agent gets from the reader about a mail
(`mx look`: a summary, a reason, a code, an amount, a deadline) is written by the reader and
checked against the mail: the summary and the reason are one short line each, and a code, amount
or deadline that the mail itself does not contain is dropped. Nothing is sent without the
confirmation code from the chat: you read the draft there and type the code back. Passwords are
typed in the server terminal and stay outside the hub.

Passwords, drafts and the alert log are kept per machine, outside the hub. `mail/` in your hub
holds the mailbox list (addresses and folder names), your sorting rules and the settings, with
their history in git. Removing the app leaves `mail/` as it is.

## Develop

```
uv run pytest -q
uv run ruff check .
cc-hub app install /path/to/hub-apps/ledger --link --yes
```

The last command links the working copy into a hub as an app, so edits apply at
once. `/path/to/hub-apps/ledger` is the `ledger/` directory of your clone. The
tests use synthetic bills only; some fixtures are copied from
double-entry-generator's examples (Apache-2.0, see `tests/fixtures/deb/NOTICE`).
No real bill or ledger belongs in this repository.

## License

MIT. See [LICENSE](LICENSE).

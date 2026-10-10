# Set up the ledger app

Run this from the hub root, in a direct chat with the user: you will ask about
their money. Talk to them in their language. Every step checks before it acts,
so this file can run again after an update; a step that is already done is
skipped without asking again. The software from `app.toml` is installed before
you get here. `lk` below is `uv run apps/ledger/scripts/lk.py`; its first run
downloads Python packages and can take a minute.

Every change under `ledger/` follows the write rules in
`apps/ledger/skills/ledger/SKILL.md` ("Writing to the ledger"): it ends with
`bean-check ledger/main.beancount`, which must pass, and a commit of `ledger/`
only: `git add -- ledger && git commit -m "ledger: <what>" -- ledger`. Commit
only when `git status --porcelain -- ledger` lists something: on a re-run there
can be nothing to commit, and `git commit` then fails.

## 1. Settings

Skip this step when `start` under `[ledger]` in `ledger/config.toml` is not
empty. Otherwise the books are new, or an earlier run stopped part way: keep
what is already written, as the checks below say.

Ask in a few short messages, not one long form. Never guess an answer.

- **Currency.** The main currency, a code such as CNY or USD (the seed says CNY).
- **Cards.** Each bank card: the bank, debit or credit, the last four digits.
- **Sources.** The bill sources they export, from `alipay`, `wechat`, `meituan`,
  `jd`, `ccb`, `icbc`, `bosc`, `nbcb` (the guides in
  `apps/ledger/skills/ledger/sources/`). A bank without a guide is not listed
  now; `import.md` ("A bank the app does not know") handles it when they bring
  a statement.
- **Wallets.** For each platform source (alipay, wechat, meituan, jd): which of
  the payment methods in the "Convert" table of its guide they use.
- **Start and balances.** The first day of the books (YYYY-MM-DD) and the
  balance of every account at the start of that day: the cards, the wallets,
  cash, and money lent to or borrowed from people
  (`Assets:Receivables:Loans:<Name>`, `Liabilities:Payables:<Name>`, named as in
  `record.md`). Money they owe, on a credit card or Huabei, is negative. Bills
  dated before this day are not imported. If they do not know a balance, leave
  that account out of the opening entry and say so; do not estimate. Holdings
  they already own are booked in the same entry with their cost
  (`investments.md`).
- **Chat defaults.** The account a chat entry goes to when they name none, and,
  for each of WeChat and Alipay (and Meituan and JD if they use them), the
  account that "paid with WeChat" usually means. `Assets:WeChat:Change` and
  `Assets:Alipay:Balance` are the usual answers.
- **Phone.** Whether they want to open Fava on their phone, which needs
  Tailscale on this machine and on the phone (step 3).

Then write, in this order. Skip whatever is already there.

1. **Accounts.** For each account named above, append `<start> open <account>`
   to `ledger/accounts.beancount`, unless an `open` for it is already there.
   Cards are `Assets:Bank:<BANK>:<tail>` or `Liabilities:CreditCard:<BANK>:<tail>`
   with `<BANK>` the bank's short code in capitals (`CCB`, `ICBC`, `BOSC`,
   `NBCB`, or for another bank `CMB`, `BOC`, ...). The wallet accounts are the
   ones in the guides' "Convert" tables. If they export Alipay or WeChat, open
   its document account (`Assets:Alipay:Balance`, `Assets:WeChat:Change`) even
   with no balance: filing a bill needs the account open.
2. **Opening balances.** Edit `ledger/txns/opening.beancount` yourself (`lk add`
   would tag the entry as a chat entry and file it under the year). Write one
   transaction dated the start day, with a posting for each account that has a
   balance and no amount on the last posting. If the file already holds a
   transaction, show it to the user and ask whether to replace it.

   ```
   2026-10-01 * "Opening balances"
     Assets:Bank:CCB:1234                5230.00 CNY
     Assets:Alipay:Balance                312.40 CNY
     Liabilities:CreditCard:CMB:5678    -1800.00 CNY
     Equity:Opening-Balances
   ```

   Add no `balance` directive for the start day: a bill entry dated that day
   would then fail the check.
3. **Importer rules.** For each platform source they export, create
   `ledger/importers/<source>.yaml` (if it exists, add only what it lacks).
   Its `rules:` list holds:
   - a rule for every card they gave, `method: "(1234)"` with `methodAccount`
     the card's account (a rule that never matches does nothing). If two cards
     share their last four digits, ask how the platform shows each, such as
     `招商银行信用卡(1234)`, and use that text, or the second rule would take
     both.
   - a rule for each payment method of the guide's "Convert" table that they
     use and the table does not mark "(default rule)": `method: <text>` with
     `methodAccount: <account>`. Alipay and WeChat need none for their wallets;
     Meituan and JD do (`美团月付`, `钱包余额`, `白条`).
   - in `meituan.yaml` and `jd.yaml` only: for each of WeChat and Alipay they
     also export, the ignore rule that guide gives under "Known issues"
     (Meituan and JD list orders paid through them, which the other bill
     books). `alipay.yaml` and `wechat.yaml` get no ignore rule.

   ```yaml
   rules:
     - method: "(1234)"
       methodAccount: Assets:Bank:CCB:1234
     - method: 美团月付
       methodAccount: Liabilities:Meituan:MonthlyPay
     - method: 微信
       ignore: true
   ```
4. **Currency in the ledger.** Set the currency in `option "operating_currency"`
   in `ledger/main.beancount`; beancount, Fava and `lk summary` read it.
5. **config.toml.** Keep its comments and other keys. Set `[ledger] currency`,
   `[chat] default`, a line per channel under `[chat.channels]`
   (`wechat = "Assets:WeChat:Change"`), `[import] sources`, and
   `[fava] tailscale` (`true` or `false`, their answer about the phone). Every
   account named there must be open. Do not set `start` yet.
6. Run `bean-check ledger/main.beancount` and fix what it reports until it
   passes.
7. Set `[ledger] start` last. It marks this step as done, so an interrupted run
   starts this step again. Commit (`ledger: set up the books`) and tell the user
   what you opened, the opening balances and the sources.

## 2. Fava

Fava runs in the background on `127.0.0.1` at `[fava] port` in
`ledger/config.toml` (5000 unless changed). Find the platform with `uname -s`;
anything that is neither Linux nor macOS is Windows below. Before you create or
change the service, tell the user what it is (a background service of their
account that listens on this machine only) and get their OK.

**Linux.** If `systemctl --user show-environment` fails (no systemd user
manager, as in a container or WSL without systemd), do what Windows does below.

1. `uv run apps/ledger/scripts/lk.py fava-unit --platform linux --ledger "$(pwd -P)/ledger/main.beancount"`
   prints the unit file. If it fails, read the `lk:` message; `fava is not on
   PATH` means the software step is not finished.
2. If `~/.config/systemd/user/cc-hub-ledger-fava.service` is missing or differs
   from that text, create its folder, save the text there and run
   `systemctl --user daemon-reload`.
3. If `systemctl --user is-enabled cc-hub-ledger-fava` fails, run
   `systemctl --user enable --now cc-hub-ledger-fava`. If it was already
   enabled and you saved a changed unit, run
   `systemctl --user restart cc-hub-ledger-fava`.
4. If `loginctl show-user "$USER" -p Linger` does not print `Linger=yes`, Fava
   stops when the user logs out. With their OK, try `loginctl enable-linger`.
   If it needs root, tell the user to run `sudo loginctl enable-linger $USER`.
   If they decline, or it cannot be done, tell them that Fava stops when they
   log out and starts again when they log in, and that they can run the command
   later. Then carry on: this does not hold up the setup.

**macOS.**

1. `uv run apps/ledger/scripts/lk.py fava-unit --platform macos --ledger "$(pwd -P)/ledger/main.beancount"`
   prints the plist.
2. If `~/Library/LaunchAgents/io.cc-hub.ledger.fava.plist` is missing or
   differs from that text: when `launchctl print gui/$(id -u)/io.cc-hub.ledger.fava`
   succeeds, run `launchctl bootout gui/$(id -u)/io.cc-hub.ledger.fava`; save
   the text there; run `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.cc-hub.ledger.fava.plist`.
3. If the plist is as it should be but `launchctl print` fails, run the
   `bootstrap` command.

**Windows, or no systemd.** Nothing to install. Tell the user that Fava does not
stay running here: when they want to see the books you start it in the
background (`apps/ledger/skills/ledger/fava.md`).

**Check** (Linux and macOS): Fava answers for this ledger when
`curl -sL --noproxy '*' --connect-timeout 5 --retry 5 --retry-connrefused http://127.0.0.1:<port>/ | grep -c "$(pwd -P)/ledger/main.beancount"`
prints a number above 0. A status code alone proves nothing: another program
that holds the port answers too, while Fava fails to start. If it prints 0,
read `journalctl --user -u cc-hub-ledger-fava -n 30` (Linux) or run
`launchctl print gui/$(id -u)/io.cc-hub.ledger.fava` (macOS) and fix it. When
the port is taken (the log says `port <port> is already in use`), change
`[fava] port`, commit, and repeat this step.

When you created or changed the service, or on Windows, tell the user how to
open Fava on a computer: `ssh -L <port>:localhost:<port> <user>@<server>`, then
`http://localhost:<port>` in the browser (`<user>` and `<server>` are their
login and the server's address as they reach it from outside).

## 3. Tailscale (optional)

Skip this step unless `[fava] tailscale` in `ledger/config.toml` is `true`, the
answer recorded in step 1. If the user later asks for Fava on their phone, set
it to `true`, commit, and do this step.

Fava on a phone goes through Tailscale: `tailscale serve` shows Fava to the
user's own tailnet over HTTPS, nothing public.

1. If `command -v tailscale` finds nothing, or `tailscale status` says it is
   stopped or logged out, you cannot fix it: it needs root and the user's
   login. Give the user the steps: install
   (`curl -fsSL https://tailscale.com/install.sh | sh`, other systems at
   https://tailscale.com/download), then `sudo tailscale up`, which prints a
   login link. Note it as left for the user (step 4) and skip the rest of this
   step.
2. Run `tailscale serve status`. If it already proxies `/` to
   `http://127.0.0.1:<port>`, this step is done. If it serves something else on
   https port 443, ask the user before replacing it. Otherwise run
   `tailscale serve --bg <port>`.
   - `Access denied` means Tailscale needs `sudo tailscale set --operator=$USER`
     once. A message with a link to enable Serve or HTTPS for the tailnet means
     the user must open that link. Give the user either, note it as left (step
     4) and skip the rest of this step.
3. Tell the user the address from `tailscale serve status` (it looks like
   `https://<machine>.<tailnet>.ts.net`), and that the phone needs Tailscale
   installed and logged in to the same account. Never use `tailscale funnel`:
   it publishes to the internet. On Windows Fava answers only while you have
   started it.

## 4. What is left for the user

If step 3 left something only the user can do (installing Tailscale, logging in,
enabling Serve), tell them the exact commands, run
`cc-hub app setup-state ledger waiting --note "<what is left>"`
(for example `--note "install Tailscale and run sudo tailscale up"`), and stop:
do not register the scheduled tasks or mark the app done. When the user says it
is done, run this file again from the top; finished steps are skipped. Linger
(step 2) is not one of these: it never holds the setup up.

When nothing is left, the scheduled tasks and the rest are the `hub-apps`
skill's job.

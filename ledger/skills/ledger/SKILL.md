---
name: ledger
description: "Keep the user's books with beancount: record what they spend or earn from chat, import bills and bank statements, answer questions about their money, send charts, and run Fava. Use for anything about the user's money, bills, accounts, loans, reimbursements or investments."
---

# ledger

The user's books are a beancount ledger in `ledger/` at the hub root. Run
every command from the hub root. `lk` below stands for
`uv run apps/ledger/scripts/lk.py`; write it out in full when you run it.
Every `lk` command except `table` and `fava-unit` takes `--ledger-dir ledger`.
Commands print JSON; on failure they print `lk: <message>` on stderr and exit
1, and the message says what is wrong.

If `start` in `ledger/config.toml` is empty, the app is not set up yet: follow
`apps/ledger/SETUP.md` first.

## What lives where

| Path | What |
|---|---|
| `ledger/main.beancount` | options and includes; `bean-check ledger/main.beancount` checks the whole ledger |
| `ledger/accounts.beancount` | `open` and `commodity` directives, including the default categories |
| `ledger/txns/<year>.beancount` | transactions, one file per year; `lk` creates a new year's file |
| `ledger/txns/opening.beancount` | opening balances |
| `ledger/txns/web.beancount` | entries added in Fava |
| `ledger/prices.beancount` | `price` directives, appended by the `prices` task |
| `ledger/config.toml` | settings: currency, chat accounts, bill sources, Fava port |
| `ledger/importers/<source>.yaml` | the user's converter rules for one bill source |
| `ledger/documents/` | original bills, filed by account |
| `ledger/inbox/` | bills being imported, with their staged entries and plans; not in git |
| `apps/ledger/importers/` | the app's default rules; never edit them |

## Writing to the ledger

1. Before your first change, run `git status --porcelain -- ledger`. If it
   lists changes you did not make (edits made in Fava, or by hand), commit
   them on their own first:
   `git add -- ledger && git commit -m "ledger: edits made outside the chat" -- ledger`
2. Change the ledger only with `lk add` or `lk apply`, or by editing an entry
   or directive in place and then running `bean-check ledger/main.beancount`.
   `lk add` and `lk apply` check the ledger themselves and undo a write that
   adds an error. After a hand edit, fix or undo whatever bean-check reports;
   never leave a new error behind.
3. When the task is done, commit only `ledger/`:
   `git add -- ledger && git commit -m "ledger: <what>" -- ledger`, for
   example `ledger: lunch 45.00 on 2026-10-10` or `ledger: import alipay 2026-09`.
   Do not push; the hub's nightly task does.
4. The `prices` task may commit at the same time (it writes only
   `ledger/prices.beancount`). If git reports `index.lock`, wait a few seconds
   and run the same git command again.

## Privacy

- Talk about money only in a direct chat with the user. In a group chat, say
  that the books are discussed in a direct chat only, and stop.
- Never write a password, a bill or zip password, or login details to any
  file, memory log or commit message. A password the user gives you is for the
  current run only.
- Ledger data stays in the hub, the chat and the hub's own git remote. Never
  paste it into a website or another service.

## Talking to the user

- Reply in the user's language. Name accounts as they are written in the
  ledger.
- After a write, show what you wrote: the entry, or the counts of an import.
- Never guess an amount, a date or an account. When an entry would need a
  placeholder account (`Expenses:Unsorted`, `Income:Unsorted`,
  `Assets:Unmapped`), or `lk match` reports an `uncertain` item, ask the user.
  Placeholders are never opened and never written to the ledger.

## Reference

| When the user wants to | Read |
|---|---|
| record, correct or delete an entry; lend, borrow, get reimbursed; open an account | [record.md](record.md) |
| import a bill or statement; month-end balances and summary; add a bank | [import.md](import.md), then the source guide `sources/<source>.md` |
| know something about their money | [query.md](query.md) |
| get a chart | [charts.md](charts.md) |
| buy or sell shares or funds; prices | [investments.md](investments.md) |
| open Fava, or Fava is not working | [fava.md](fava.md) |

Source guides: [alipay](sources/alipay.md), [wechat](sources/wechat.md),
[meituan](sources/meituan.md), [jd](sources/jd.md), [ccb](sources/ccb.md),
[icbc](sources/icbc.md), [bosc](sources/bosc.md) (Bank of Shanghai),
[nbcb](sources/nbcb.md) (Bank of Ningbo).

# Rules: what is pushed, the digest, how often mail is checked

## The three layers

1. **Fixed rules**, the same for everyone: the "Fixed rules" of
   [SKILL.md](SKILL.md). No setting changes them; when the user asks for
   something against them (send without showing the draft, delete without a
   list), say that the app does not do that.
2. **The user's rules**, in `mail/config.toml`: `[settings]` for every
   mailbox, and a mailbox's own section, which overrides `[settings]` for that
   mailbox.
3. **Defaults**, what applies until the user changes something:
   - important: mail the user must reply to or act on themselves, and mail
     about money or account security (bills, charges, sign-in alerts);
   - important mail is pushed at once (`delivery = "instant"`);
   - the digest every day at 08:00 in the user's local time (setup converts
     it to the server's time);
   - mail is checked every 5 minutes.

When a mail is judged, the mailbox's own value wins over `[settings]`, which
holds the defaults until the user changes it. `muted_senders` and `notes` of
the mailbox and of `[settings]` both apply.

## The settings in `mail/config.toml`

`[settings]`, for every mailbox:

| Key | Value |
|---|---|
| `important` | one sentence: what counts as important. The agent judges each mail against it; it is not a keyword list |
| `delivery` | `"instant"`: important mail is pushed at once; `"digest"`: it waits for the digest, at the top |
| `muted_senders` | addresses (`news@shop.example`) or whole domains (`@ads.example`, not its subdomains) whose mail is never pushed, only counted in the digest. The background check sorts it out without waking the agent |
| `notes` | corrections the user gave in chat, one line each; they refine `important` |

A mailbox section `[mailboxes."工作"]` may set `important`, `delivery`,
`muted_senders` and `notes` for that mailbox alone. Its other keys (`address`,
`provider`, `imap`, `smtp`, `added`) are written by `account add`: leave them.
`paused = true` (its mail goes only to the digest) is set with `account edit --pause`
([mailboxes.md](mailboxes.md)). The times of the check and of the digest are
not in this file (below).

Write the user's own words, in their language: a rule is quoted to you when
mail is judged, and the user may read and edit the file. An example, a Feishu
work mailbox that pushes only mail addressed to the user that needs them:

```toml
[mailboxes."工作"]
address = "me@corp.example"
provider = "feishu"
imap = "imap.feishu.cn:993"
smtp = "smtp.feishu.cn:465"
added = "2026-10-14T09:00:00+08:00"
important = "直接发给我（我在收件人里，不是抄送、不是群发列表）、需要我回复或处理的信"
delivery = "instant"
```

To change the file:

1. Edit it in place with your file tool. Change only the keys you mean to;
   keep its comments and every other line. Text goes in double quotes; write
   a `"` inside as `\"` and a `\` as `\\`. Lists are `["a", "b"]`.
2. Check it: `uv run apps/mail/scripts/mk.py config check` prints
   `{"ok": true, "mailboxes": [...]}`, or `mk: config.toml: ...` naming the bad
   key. Fix it until it passes: while the file is broken, no mailbox is
   checked.
3. Tell the user the rule in one sentence and which mailbox it governs (or
   "all mailboxes").
4. Commit only `mail/`:
   `git add -- mail && git commit -m "mail: push only direct mail for 工作" -- mail`.

## Corrections in the chat

"这类以后要推", "这种别推了", "don't push newsletters":

- They apply to the mailbox of the mail they are about. Only when the user
  says so ("所有邮箱都这样", "for all mailboxes") do they go into `[settings]`.
- About one sender ("别推他的信"): add the address, or `@domain` for a whole
  domain, to `muted_senders`. Their mail then never wakes the agent.
- About a kind of mail: add one line to `notes`, in the user's words, such as
  `"GitHub 的通知不用推，CI 失败的除外"`.
- A change of what counts as important: rewrite `important`, with the user.

Then check, restate and commit as above.

## Conflicts: point them out when the setting is made

Some settings together mean that mail never reaches the user, or not when
they think. Say so at once, when the second one is set, and ask how they want
it:

- **The digest is off** while a mailbox (or `[settings]`) has
  `delivery = "digest"`, a mailbox is paused, or senders are muted: that mail
  goes only to the digest, so it reaches the user nowhere. Whether the digest
  is on: `cc-hub cron list --json`, the row whose `name` is `mail/digest`, its
  `enabled`.
- **The check runs rarely** (once a day, say) while delivery is `instant`:
  important mail is pushed only as often as the check runs. The row
  `mail/poll` gives the `schedule`.
- **The check is off** (`mail/poll` with `enabled: false`): nothing is pushed
  at all; mail is checked only when the digest runs.
- **The triage task is off** (`mail/triage`): nothing is pushed; the digest
  still judges and lists the mail.

## How often mail is checked, when the digest comes

These are scheduled tasks, kept in `hub.toml`:

| Command | What it does | Example |
|---|---|---|
| `cc-hub cron set mail/poll "<cron>"` | how often mail is checked | `cc-hub cron set mail/poll "*/15 * * * *"` (every 15 minutes) |
| `cc-hub cron set mail/digest "<cron>"` | when the digest is sent | `cc-hub cron set mail/digest "30 7 * * 1-5"` (07:30 on weekdays) |
| `cc-hub cron set mail/digest --off` | no digest; `--on` turns it on again | `cc-hub cron set mail/digest --off` |
| `cc-hub cron reset mail/poll` | back to the app's time (every 5 minutes; the digest: 08:00) | `cc-hub cron reset mail/digest` |

`<cron>` is a five-field cron expression (minute, hour, day of month, month,
day of week), always in double quotes. `mail/triage` has no time, only `--off`
and `--on`. Each command commits `hub.toml` and prints the steps that register
the task anew: follow them in a direct chat, as "Change a time" in
`cron/README.md` says. Never change a time in cc-connect alone.

- **Time zone.** A schedule runs in the server's time zone. Compare it
  (`date +%Z%z`, or `timedatectl` on Linux) with the user's. When they
  differ, tell the user, convert the time they asked for into server time,
  and say both: "08:00 Beijing time is 00:00 on the server (UTC)". When the
  conversion crosses midnight, shift the day-of-week field too. Where the
  user's zone (or the server's) has daylight saving time, the digest comes an
  hour off for part of the year: tell them.
- **How often.** Every check that finds mail to judge starts one agent run,
  so the frequency is also the most the agent can be woken for mail: a lower
  frequency costs less and pushes later. A check more often than every 5
  minutes gains little. After a change of frequency, `stale` in `bg status`
  can be wrong for a few checks ([SKILL.md](SKILL.md)).
- After a change, restate it in the user's words and point out any conflict
  above.

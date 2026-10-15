# Set up the mail app

Run this from the hub root, in a direct chat with the user: you will ask
about their mail and receive a password. Talk to them in their language.
Every step checks before it acts, so this file can run again after an update;
a step that is already done is skipped without asking again. `uv`, from
`app.toml`, is installed before you get here. The first run of
`uv run apps/mail/scripts/mk.py` downloads Python packages and can take a
minute.

## 1. cc-connect

Run `cc-connect --version`. If it is older than 1.5.1, tell the user: files
over 2 MB that they send to the chat from Feishu do not reach you, so such a
file (a CV as a PDF, say) cannot be attached to a mail until cc-connect is
1.5.1 or newer. This does not stop the setup. If there is no `cc-connect` at
all, step 4 cannot be done; say so now.

## 2. The defaults and the times

Skip steps 2 and 3 when `uv run apps/mail/scripts/mk.py config check` lists a
mailbox: the user has been through them.

Tell the user the defaults and ask once whether to change any:

- important: mail they must reply to or act on themselves, and mail about
  money or account security (bills, charges, sign-in alerts);
- important mail is pushed to the chat at once; the rest waits for the digest;
- the digest every day at 08:00, their local time;
- new mail is checked every 5 minutes.

A change to what is important, or to how it is delivered, goes into
`[settings]` of `mail/config.toml`, as `apps/mail/skills/mail/rules.md` says
(check it, commit `mail/`).

The times are the tasks `mail/poll` and `mail/digest`, and they run in the
server's time zone:

1. Find it with `date +%Z%z` (`timedatectl` on Linux shows its name). Take the
   user's time zone from `USER.md`, or ask.
2. If the two differ, tell the user, and convert the digest time into server
   time: 08:00 in Beijing (UTC+8) is 00:00 on a UTC server. When the user's
   zone, or the server's, has daylight saving time, tell them that the digest
   comes an hour earlier or later for part of the year.
3. When the digest time in server time is not 08:00, set it, with the
   expression in quotes: `cc-hub cron set mail/digest "0 0 * * *"`. No digest
   at all: `cc-hub cron set mail/digest --off`.
4. When the user wants mail checked at another frequency, set it:
   `cc-hub cron set mail/poll "*/10 * * * *"`. Every check that finds mail to
   judge wakes the agent once, so this is also the most often that happens.

Each command commits `hub.toml` and prints steps to register the task again.
Do not run them here: step 4 registers the tasks as `hub.toml` now says.

## 3. The first mailbox

Follow "Add a mailbox" in `apps/mail/skills/mail/mailboxes.md` (it links the
provider guides). Its rules step is done: step 2 told the defaults. If the
user has more mailboxes, add them now too; for each, ask whether it needs
rules of its own.

If no mailbox can be added now (the provider is not supported, or the user
first needs something from someone else, such as their Feishu admin), tell
them what is missing, run
`cc-hub app setup-state mail waiting --note "add a mailbox: <what is missing>"`
(for example `--note "add a mailbox: the Feishu admin must allow mail clients"`)
and stop. When they are ready, run this file again.

## 4. The scheduled tasks

| Task | Kind | What it does |
|---|---|---|
| `mail/poll` | script, every 5 minutes | checks every mailbox for new mail, without the agent; wakes `mail/triage` |
| `mail/triage` | on demand, restricted | judges new mail, pushes what is important, reports mailbox problems |
| `mail/digest` | daily at 08:00, restricted | the daily digest |

Registering tasks is the step "Scheduled tasks" of the `hub-apps` skill:
`cron/README.md`, "Register", for the rows of `cc-hub cron list --json` whose
`source` is `mail`. It comes after this file; do it now, so that step 5 can
check the first poll. When the `hub-apps` skill reaches it, there is nothing
left to change. If `cc-connect` is not available, run
`cc-hub app setup-state mail waiting --note "register the mail tasks: cc-connect is needed"`
and stop.

What `cron/README.md` has you tell the user, for this app:

- **`mail/poll`** is a script task. Show its command,
  `uv run apps/mail/scripts/mk.py poll --wake`, and what the body of
  `apps/mail/cron/poll.md` says it does. If the user declines, do not register
  it, run `cc-hub cron set mail/poll --off` so that it is not asked again and
  the app knows the check is off by choice, and tell them: without the check
  no mail is pushed, and mail is checked only when the digest runs. They can
  turn it on later (`cc-hub cron set mail/poll --on`, then register it). Skip
  step 5.
- **`mail/triage`** and **`mail/digest`** read mail, so they are restricted to
  the app's `mk.py bg` commands. On a runtime without the guard, if the user
  declines one, turn it off (`cc-hub cron set mail/triage --off`, or
  `mail/digest`) and tell them what is lost: without `triage` nothing is
  pushed (the digest still judges the mail); without `digest` there is no
  daily summary, and mail that only goes to the digest (muted senders, paused
  mailboxes, mailboxes set to digest delivery) reaches them nowhere.

## 5. The first check

Skip this step when `mail/poll` is off. Otherwise the setup is done only when
the background check has really run:

1. Run `uv run apps/mail/scripts/mk.py bg status`. If `last_poll` is set and
   `stale` is false, the check runs: this step is done.
2. Note the time (`date -u +%Y-%m-%dT%H:%M:%SZ`). Find the id of
   `hub:mail/poll` with `cc-connect cron list`, and run
   `cc-connect cron exec <id>`: it runs the task now.
3. Run `uv run apps/mail/scripts/mk.py bg status` again: `last_poll` must be
   later than the time you noted. A check takes from a few seconds to a few
   minutes (a first check of a big inbox takes longer): while it is not there
   yet, run `bg status` again a few times over the next two minutes or so.
4. If it still did not run, the cc-connect daemon most likely does not find
   `uv`, `cc-hub` or `cc-connect` on its PATH. "Script tasks and PATH" in
   cc-hub's
   [docs/cc-connect.md](https://github.com/Jenwein/cc-hub/blob/main/docs/cc-connect.md#script-tasks-and-path)
   says how to check and fix it. A fix usually reinstalls or restarts the
   cc-connect service, which ends this chat: give the user the steps for this
   machine and let them run them. Then run
   `cc-hub app setup-state mail waiting --note "the mail check does not run: PATH of the cc-connect daemon"`
   and stop. When they are back, run this file again.

The first check pushes nothing: the mail that was in the inbox before is only
indexed for search.

## 6. Tell the user

What happens from now on, in a few lines: important mail is pushed within
minutes of arriving (or at the frequency they chose); the digest comes at the
time they chose, in their time; they can ask about, search, archive, delete
and write mail in the chat, and nothing is sent or deleted without their
word. The `hub-apps` skill then finishes the setup.

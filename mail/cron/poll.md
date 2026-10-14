---
schedule: "*/5 * * * *"
default: true
exec: "uv run apps/mail/scripts/mk.py poll --wake"
timeout_mins: 10
---
Checks every mailbox for new mail, without the agent.

For each mailbox in `mail/config.toml` it logs in to the IMAP server with the
password stored in `secrets/mail/`, opens INBOX read-only and copies the mail
that arrived since the last check into the app's local index in `mail/.state/`.
It changes nothing on the server: no mail is marked read, moved or deleted, and
nothing is sent. Mail from a muted sender, from a paused mailbox, or from a
mailbox whose important mail waits for the digest goes straight to the daily
digest.

When other new mail is waiting for a verdict, or a mailbox has a new problem
(a refused password, a server that does not answer, a broken
`mail/config.toml`), it wakes the `mail/triage` task with
`cc-hub cron wake mail/triage`; the agent then judges the mail and pushes what
is important. Mail that was already in the inbox when a mailbox was added is
only indexed for search, never pushed.

It runs every 5 minutes unless the user picks another time
(`cc-hub cron set mail/poll "<cron expression>"`). Each wake starts one agent
run, so this frequency is also the most often the agent can be woken for
mail. It needs `uv`, `cc-hub` and `cc-connect` on the PATH of the cc-connect
daemon. What it prints goes nowhere; `uv run apps/mail/scripts/mk.py bg status`
shows when it last ran and whether its wakes reach `mail/triage`. When triage
is woken six times in a row without judging the mail, it is woken once an hour
instead, until mail is judged again.

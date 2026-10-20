# Alerts and reports

## Urgent-mail alerts

Alerts are optional and off until the user turns them on. A background service on one machine, the
one that runs cc-connect, keeps a connection to every inbox. When mail arrives, a reader with no
tools sorts it by the rules ([rules.md](rules.md)). Each mail in the `now` tier makes one message
in the user's private chat: the mailbox, the sender, the subject, why it matters and the amount or
deadline, with a warning when the mail looks like phishing. Alerts never reach you and never wake
an agent.

- A mail is alerted once. More than three urgent mails in one batch make one combined message.
- The service also checks every inbox every 15 minutes, in case a connection died unnoticed or the
  machine was off. Mail that arrived meanwhile is found and alerted in one message; after a very
  long break only the newest 200 mails of an inbox are looked at.
- When cc-connect is not running, alerts wait in a queue and go out when it is back.
- The user is told once when a mailbox cannot sign in, and once when the listener has been down
  for a while.

### Status

```
uv run apps/mail/scripts/mx.py alerts-status
```

| Field | Meaning |
|---|---|
| `enabled`, `host` | whether alerts are on, and the machine they were turned on for |
| `this_host` | this machine; when it differs from `host`, alerts are meant for the other machine. A machine is named `<hostname>#<id>`, the id kept in that machine's mail state, so Windows and the WSL inside it, which share a hostname, are two machines |
| `watching` | the watcher is running. `false` means the service is not running or its last sign of life is older than 2 minutes |
| `carillon` | `up` or `down`: the listener the watcher runs; `null` when the watcher is not running |
| `last_sweep` | when every inbox was last checked |
| `queued_alerts` | alerts waiting because the chat could not be reached |
| `problems` | mailbox problems that go on, such as a sign-in that fails |

When alerts do not arrive, see [troubleshoot.md](troubleshoot.md).

### Turn alerts on

Run these on the machine that runs cc-connect, and ask the user first: they download a program and
start a background service of the user's account.

1. `mx install-tool carillon` downloads the pinned carillon from its official release, checks its
   SHA-256, and puts it in `.local/bin` of the user's home directory (once per machine).
2. `mx alerts on` turns alerts on for this machine: it records the machine (`this_host` of
   `alerts-status`) as `host` in `mail/config.toml`, so run it on the right machine. The first
   check afterwards only sets a starting point: mail that came while alerts were off is not
   alerted. The daily report lists it.
3. Commit `mail/config.toml`, and do not push (the hub's nightly task pushes what is committed).
   The nightly task commits only the memory files, so this file would stay changed, and `host` is
   how other copies of the hub know which machine runs the alerts; a copy that never gets the
   change would keep the old switch and could send every alert twice. If git says there is
   nothing to commit, the switch was already in that state.

   ```
   git add -- mail/config.toml && git commit -m "mail: alerts on" -- mail/config.toml
   ```

4. The first time, install the service. `apps/mail/SETUP.md`, step 5, does this for each system:
   `mx watch-unit --platform linux`, `macos` or `windows` prints the definition, and the setup
   steps save it where the system wants it. Later, only start the service again (see "Stop and
   start the service").
5. `mx alerts-status` shows `watching: true` within two minutes. If it does not, read
   [troubleshoot.md](troubleshoot.md).

```
uv run apps/mail/scripts/mx.py install-tool carillon
uv run apps/mail/scripts/mx.py alerts on
uv run apps/mail/scripts/mx.py alerts-status
```

### Stop and start the service

| System | Stop it, and keep it from starting at login | Start it again |
|---|---|---|
| Linux, WSL | `systemctl --user disable --now cc-hub-mail-watch` | `systemctl --user enable --now cc-hub-mail-watch` |
| Windows | `schtasks /End /TN cc-hub-mail-watch`, then `schtasks /Change /TN cc-hub-mail-watch /DISABLE` | `schtasks /Change /TN cc-hub-mail-watch /ENABLE`, then `schtasks /Run /TN cc-hub-mail-watch` |

On macOS, `bootout` stops the job, but the property list stays in `~/Library/LaunchAgents` and
loads again at the next login, so the job must also be disabled; and a disabled job cannot be
loaded until it is enabled again:

- Stop: `launchctl bootout gui/$(id -u)/io.cc-hub.mail.watch`, then
  `launchctl disable gui/$(id -u)/io.cc-hub.mail.watch`.
- Start: `launchctl enable gui/$(id -u)/io.cc-hub.mail.watch`, then
  `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.cc-hub.mail.watch.plist` (when it
  says the job is already loaded, go on), then
  `launchctl kickstart gui/$(id -u)/io.cc-hub.mail.watch`.

After starting, `mx alerts-status` shows `watching: true` within two minutes.

The service runs `mx watch`. Never run `mx watch` yourself: in the foreground it blocks the shell
and holds the watcher's lock, and the real service then fails with `mx watch is already running
for this hub`. Where alerts are off, or are meant for another machine, `mx watch` says why on
stderr and ends at once with status 0, so a service that is left installed does nothing.

### Turn alerts off

1. Stop the service (above): a watcher that still runs keeps alerting and keeps writing its sign
   of life.
2. Switch alerts off, then commit `mail/config.toml` as when turning alerts on:

   ```
   uv run apps/mail/scripts/mx.py alerts off
   git add -- mail/config.toml && git commit -m "mail: alerts off" -- mail/config.toml
   ```

To remove the service for good, follow `apps/mail/REMOVE.md`.

## Daily and monthly reports

The mail app adds a section to the hub's daily report and one to the monthly report. cc-hub runs
the app's report command itself and pastes its text into the report as it is, so the section never
passes through you.

- The daily section lists the inbox mail that arrived since the last report (24 hours for the
  first), the `daily` tier grouped by kind, one line per mail with the mailbox, the sender, a
  one-line summary and a deadline or amount. When alerts are off, or an alert did not go out, the
  urgent mail comes first. It also says when a mailbox could not be read.
- The monthly section says whether newer versions of himalaya, carillon and ortie are out, how
  long alerts were down this month, and which mailboxes had sign-in problems.
- The user turns the sections on, and approves their commands, with the `hub-apps` skill (its
  report steps); if a section is missing from a report, start there.
- Do not run `mx report` yourself: it keeps its own place in the mail state, and a run by hand
  would change what the next report covers.

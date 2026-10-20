# Remove the mail app

Follow this before `cc-hub app remove mail --yes`: the commands below live in the app, and the
removal deletes it. Each step checks first, so a part that is already gone is skipped. The app
registers no scheduled task of its own; the report sections are dropped by the `hub-apps` skill,
not here. Run the commands from the hub root, and write the app's command line out in full as
shown.

## 1. Stop and remove the alert service

Do this first: a watcher that still runs keeps alerting and keeps its files open. Check the system
with `uname -s` (`Linux`, `Darwin` for macOS; anything else is Windows).

- **Linux, WSL.** If `~/.config/systemd/user/cc-hub-mail-watch.service` exists, run
  `systemctl --user disable --now cc-hub-mail-watch`, delete the file, and run
  `systemctl --user daemon-reload`. If `systemctl --user` cannot be reached, delete the file only.
- **macOS.** If `~/Library/LaunchAgents/io.cc-hub.mail.watch.plist` exists, run
  `launchctl bootout gui/$(id -u)/io.cc-hub.mail.watch` (it is fine if that says the job is not
  loaded), then delete the plist.
- **Windows.** In PowerShell or `cmd` (Git Bash rewrites arguments that start with `/`): if
  `schtasks /Query /TN cc-hub-mail-watch` succeeds, run `schtasks /End /TN cc-hub-mail-watch` (it
  is fine if the task is not running), then `schtasks /Delete /TN cc-hub-mail-watch /F`.

Do not turn off lingering (`loginctl disable-linger`): cc-connect may need it. This step touches
this machine only.

## 2. Switch alerts off

The alert switch is in `mail/config.toml`, which every copy of the hub shares through git, so
switching it off here would also switch off the machine that sends the alerts. Look first:

```
uv run apps/mail/scripts/mx.py alerts-status
```

If `enabled` is true and `host` is not `this_host`, alerts run on another machine (`host`; WSL and
the Windows around it are two machines). Skip
`alerts off` and its commit, and tell the user that alerts stay on for that machine; to stop them
they follow "Turn alerts off" in `apps/mail/skills/mail/alerts.md` there. Otherwise alerts are
already off or they are on for this machine: run

```
uv run apps/mail/scripts/mx.py alerts off
```

It is safe when alerts are already off. It sets `enabled` to false and clears `host` in
`mail/config.toml`, and removes the watcher's sign of life. Commit `mail/config.toml` only when
`git status --porcelain -- mail/config.toml` lists it, and do not push:

```
git add -- mail/config.toml && git commit -m "mail: alerts off" -- mail/config.toml
```

## 3. Passwords and the machine's mail state

The mail state is a folder outside the hub, per machine: the passwords and Outlook sign-ins, the
drafts waiting for a code, the generated configs and the alert log. Ask the user whether to delete
the saved passwords and Outlook sign-ins too.

- Yes: `uv run apps/mail/scripts/mx.py forget --secrets` empties the whole folder.
- No: `uv run apps/mail/scripts/mx.py forget` deletes the rest and keeps `secrets/` and the id
  that names this machine, so a later install of the app signs in at once.

Tell them that a password deleted here still works at the provider until they revoke it there:
the authorization code, app password or client password in the mailbox's account settings, and for
Outlook the app's permission in their Microsoft account.

## 4. The software

The `hub-apps` skill has you ask whether to uninstall the software the app used. If the user says
yes, tell them what each command does and run it only once they agree. The app downloaded
himalaya, and carillon and ortie when alerts or an Outlook mailbox needed them, into `.local/bin`
of the home directory (`%USERPROFILE%\.local\bin` on Windows). Delete the ones that are there:

- Linux, macOS: `rm ~/.local/bin/himalaya ~/.local/bin/carillon ~/.local/bin/ortie`
- Windows: delete `himalaya.exe`, `carillon.exe` and `ortie.exe` from `%USERPROFILE%\.local\bin`.

Leave `uv` and `claude`: the hub and the user need them for other things.

## 5. Tell the user

The mailbox list, their rules and the settings stay in `mail/` in the hub, with their history in
git: removing the app does not touch them. If they install the app again, `mail/` is used as it
is. The mail itself was never copied: it stays in their mailboxes.

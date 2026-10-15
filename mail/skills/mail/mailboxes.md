# Mailboxes

Each mailbox the app manages is a `[mailboxes."<name>"]` section of
`mail/config.toml`, made and changed by the `account` commands. Its password is
in `secrets/mail/<address>.toml`, never in git. To list the mailboxes:
`uv run apps/mail/scripts/mk.py account list` (10 to a page, `--page 2` for
more), with each one's `last_ok` (its last good check) and `paused`.

## Add a mailbox

1. **Which provider.** The user gives the address. Run
   `uv run apps/mail/scripts/mk.py account detect me@example.com`. It looks at
   the domain, and for a company's own domain at its MX records. Its `status`:
   - `supported`: `provider` (the key), `name` and `guide` (the provider
     guide, under `apps/mail/`).
   - `unsupported`: the address belongs to a service the app does not support
     yet; `reason` names it (Outlook, iCloud, Lark, Tencent or NetEase
     enterprise mail, ...). Tell the user so, and stop.
   - `unknown`: nothing places the domain; `reason` says why (MX records of
     the company's own mail gateway, records of two services, or no answer
     from DNS). Go on: ask the user which of the supported providers holds
     the mailbox (NetEase 163, 126 or yeah.net, QQ, Gmail or Google Workspace,
     Feishu) and pass its key as `--provider` in step 4 (`163`, `126`,
     `yeah`, `qq`, `gmail`, `feishu`); the login check of `account add`
     confirms the answer. Only when it is none of them, tell the user that
     the mailbox is not supported yet.
2. **Where to get the password.** Read the guide (`providers/<name>.md` next to
   this file) and walk the user through "Turn on IMAP/SMTP" and "Password" in
   their language, step by step. Mail clients log in with an app password or
   authorization code, never the web sign-in password.
3. **A short name.** Agree on a short name for the mailbox with the user, such
   as "工作" or "QQ": "reply from 工作" will mean this mailbox. A Chinese name
   is fine; no quotes or backslashes.
4. **The password.** The user sends it in the chat. Pass it on stdin, never as
   an argument:

   ```
   printf '%s' '<password>' | uv run apps/mail/scripts/mk.py account add 工作 --address me@corp.example --provider feishu
   ```

   Put the password between the single quotes as it is; a single quote inside
   it is written `'\''`. Gmail shows its app password in four groups of four
   letters: pass the 16 letters without the spaces. `account add` logs in to
   the IMAP server and the SMTP server (it sends nothing), and only when both
   work it stores the password, adds the section to `mail/config.toml` and
   notes where INBOX stands. It prints `{"ok": true, "mailbox", "address",
   "provider"}`.
5. **When it fails** it prints `{"ok": false, "step", "message", "hint"}` and
   has saved nothing. Tell the user, in plain words, where it stopped and who
   can fix it:
   - `connect`: this machine did not reach the server (network, firewall,
     proxy). Nothing the user's mail settings can change.
   - `imap-login`: the address or password was refused. Usually a web
     password instead of the client password, or IMAP not turned on. For
     Feishu, the company's admin may not have allowed mail clients.
   - `smtp-login`: reading works, sending was refused: SMTP for clients is
     off.

   The `hint` names the guide; go through the steps with the user again, and
   run the command again with the new password. Any other error (`mk: ...`):
   a name or address already in use, an address the app cannot place without
   `--provider` (ask the user, as in step 1). Say what it says.
6. **Afterwards.** Never show the password again. Suggest the user recall (or
   delete) the chat message that held it. Tell them that mail already in the
   inbox is not pushed and not in the digest: only mail that arrives from now
   on is new. The mail of about the last 90 days is indexed for search over
   the next checks.
7. **Rules.** For the first mailbox, if you did not just come from
   `apps/mail/SETUP.md` (which tells the defaults), tell the user the default
   rules (see "The three layers" in [rules.md](rules.md)) and ask once whether
   to change any. For any later mailbox, ask one question: should this
   mailbox have rules of its own, such as only pushing mail sent to them
   directly? Write the answers as [rules.md](rules.md) says.
8. **Commit** only `mail/`:
   `git add -- mail && git commit -m "mail: add mailbox 工作" -- mail`.
   `mail/.gitignore` keeps the index and the downloads out of git, and the
   password is not under `mail/`.

## Change a mailbox

All of these print `{"ok": true, "mailbox", "address", "changed"}`; a refused
password prints the failure of step 5 above and changes nothing. Commit
`mail/` afterwards, as above.

- **New password** (expired, revoked, the user made a new one, or the hub
  moved to another machine, where `secrets/` does not follow, since it is not
  in git):
  `printf '%s' '<password>' | uv run apps/mail/scripts/mk.py account edit 工作 --password-stdin`.
  It is stored only after both logins work. Then suggest recalling the
  message, as above.
- **New short name:** `uv run apps/mail/scripts/mk.py account edit 工作 --rename 公司`.
- **Pause pushes** (its mail goes only to the digest):
  `uv run apps/mail/scripts/mk.py account edit 工作 --pause`; push again with
  `uv run apps/mail/scripts/mk.py account edit 工作 --resume`. A paused
  mailbox is still checked and still in the digest. If the digest is off,
  point out that its mail would then reach the user nowhere ([rules.md](rules.md)).
- **Check it** (both logins with the stored password):
  `uv run apps/mail/scripts/mk.py account test 工作`.
- **Its own rules:** [rules.md](rules.md).

An address cannot be changed: add the new address as a mailbox and remove the
old one.

## Remove a mailbox

1. Say in one message what goes and what stays, and ask the user to confirm:
   - goes: its password in `secrets/mail/`, its section and its own rules in
     `mail/config.toml`, its mail in the local index (search no longer finds
     it), its open problems;
   - stays: the attachments already downloaded from it in
     `mail/attachments/`, the rules for every mailbox (`[settings]`), and of
     course all its mail on the server.
2. After the user confirms: `uv run apps/mail/scripts/mk.py account remove 工作`.
   It prints `{"ok": true, "mailbox", "address", "password_deleted"}`. While
   a check for new mail is running it waits for it, up to a minute; when it
   says a check is still running, nothing was removed: try again a minute
   later.
3. Commit `mail/` (`mail: remove mailbox 工作`). Suggest the user also revoke the
   app password in the provider's settings (the guide says where), so it can
   no longer be used anywhere.

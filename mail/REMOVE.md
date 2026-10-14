# Remove the mail app

Follow this before `cc-hub app remove mail --yes`, in a direct chat with the
user. Each step checks first, so a part that is already gone is skipped. The
scheduled tasks (`mail/poll`, `mail/triage`, `mail/digest`) are unregistered
by the `hub-apps` skill, not here; once they are, nothing checks the
mailboxes any more and nothing is pushed.

## 1. The passwords

The passwords of the mailboxes are in `secrets/mail/`, one file per address,
outside git. Removing the app does not touch them. Ask the user whether to
delete them. Only if they agree, run `rm -r secrets/mail` from the hub root.
Do not read the files.

Either way, suggest the user revoke the app passwords and authorization codes
in each provider's settings (the provider guides in
`apps/mail/skills/mail/providers/` say where): until then they still work for
anyone who has them.

## 2. Tell the user what stays

The data stays in the hub when the app is removed:

- `mail/config.toml`: the mailboxes and the rules, in git. If the app is
  installed again, it is used as it is.
- `mail/.state/`: the local index of the mail, the drafts and the delete
  lists. Not in git. It can be deleted; a new install rebuilds the index
  when the mailboxes are checked.
- `mail/attachments/`: the attachments downloaded from mail, by address and
  month. Not in git. They stay until the user deletes them.

Neither `mail/.state/` nor `mail/attachments/` moves with the hub to another
machine: a clone of the hub has only what is in git. On a new machine the
index is rebuilt from the mailboxes, and the downloaded attachments are not
there.

Delete `mail/.state/` or `mail/attachments/` only if the user asks.

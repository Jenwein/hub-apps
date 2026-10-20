# When something fails

Read the message first: `mx: <message>` says what is wrong, and often the command that fixes it.
Never work around a failure by running himalaya, a mail program or any other tool on the mailbox
yourself, and never ask for a password to get past one.

## Check the mailboxes

```
uv run apps/mail/scripts/mx.py check
uv run apps/mail/scripts/mx.py check qq
uv run apps/mail/scripts/mx.py secret-status
```

`check` tests each mailbox (or the one named): it signs in, opens both the receiving and the
sending side, and lists one mail. The answer per mailbox is `{"ok": true}` or
`{"ok": false, "kind": "auth" | "connect" | "other", "error": "..."}`; the command itself ends with
status 0 either way. `secret-status` says per mailbox whether a password is stored (`true` or
`false`; `"oauth"` for an Outlook mailbox) and never shows it.

## The sign-in fails

`kind` is `auth`, or a list says `login failed`: the server refused the password, the
authorization code, the app password or the Outlook sign-in. It was revoked, expired or changed,
or it was never stored (`mx configure` lists such mailboxes under `missing_secrets` and
`missing_sign_in`).

Tell the user which mailbox, and that they sign in again themselves, in the server's terminal, in
the hub's folder:

- Feishu, QQ and Gmail: `uv run apps/mail/scripts/mx.py secret-set <name>`
- Outlook: `uv run apps/mail/scripts/mx.py oauth-login <name>` (with `--paste` when Microsoft
  refuses the sign-in by code)

Never ask for the password in the chat, never take one from the user in the chat, and do not run
these commands for them: they refuse to run without a person at a terminal. Afterwards run
`mx check <name>`. When the sign-in is refused again, the account side may be wrong: QQ needs an
authorization code and not the QQ password, Gmail an app password, Feishu a client password that
the admin allowed ([providers.md](providers.md)). A password that is right but refused may need
`sasl = "login"` in the mailbox's entry in `mail/mailboxes.toml`; then `mx configure`. To remove a
stored password, only when the user asks: `mx secret-delete <name>`.

## `cannot connect`

The mail server did not answer: no network, a firewall, a wrong server address, or a server that
is down. When every mailbox fails, look at this machine's network; when one does, at that provider
(its server is `imap_host` and `smtp_host` in `mail/mailboxes.toml`, or the provider's usual one).
Try again after a while before you tell the user it is broken.

An error that says the mail program is not installed means it is missing: ask the user, then
`mx install-tool himalaya` puts the pinned version in `.local/bin` of their home directory.

## The reader

`look`, `ask`, `code`, `reply`, the alerts and the daily report need the reader, which is Claude
Code (`claude`) with no tools. This is so even when the hub's own agent is another one.

```
uv run apps/mail/scripts/mx.py reader-check
```

It answers `{"ok": true}` or `{"ok": false, "detail": "..."}`. The usual causes:

- `claude` is not installed, or not on `PATH`. The user installs Claude Code with its native
  installer.
- On Windows, a `claude.cmd` from npm is refused, because it garbles the arguments. The user
  installs Claude Code with its native installer instead.
- `claude` is not signed in. The user runs `claude` once in the server's terminal and signs in.
- A usage limit, or no answer in time. Try again later.

The reader's model is `model` under `[reader]` in `mail/config.toml`.

## The private chat

`show`, `ask`, `attachment-send` and every draft write to the user's private chat.

- `no private chat recorded`: run `mx chat-set` from here, in the direct chat. It records this
  chat (cc-connect gives it to the agent as `CC_SESSION_KEY` and `CC_PROJECT`) and sends a test
  message there; tell the user it arrived. `mx chat-show` shows which chat is recorded. Run
  `chat-set` again if the user moves to another chat.
- `cc-connect is not on PATH`, `cannot run cc-connect`, or `cc-connect exited with status ...`:
  cc-connect is not installed, not running, or cannot reach the chat. It must be running. A draft
  made meanwhile is saved but its code is void: after the chat works again, `mx confirm <id>`.

```
uv run apps/mail/scripts/mx.py chat-set
uv run apps/mail/scripts/mx.py chat-show
```

## Alerts do not arrive

```
uv run apps/mail/scripts/mx.py alerts-status
```

Go through the fields ([alerts.md](alerts.md) explains them):

1. `enabled` is false: alerts are off. `host` is not `this_host`: alerts were turned on for another
   machine; they run there.
2. `watching` is false: the service is not running here. Look at it:

   | System | Status and log |
   |---|---|
   | Linux, WSL | `systemctl --user status cc-hub-mail-watch`, `journalctl --user -u cc-hub-mail-watch -n 50 --no-pager` |
   | macOS | `launchctl print gui/$(id -u)/io.cc-hub.mail.watch`; the service writes its output to `watch.log` |
   | Windows | `schtasks /Query /TN cc-hub-mail-watch /V /FO LIST`; start it with `schtasks /Run /TN cc-hub-mail-watch` |

   A Linux user service stops when the user logs out unless lingering is on (`loginctl
   enable-linger`; step 5 of `apps/mail/SETUP.md`). On WSL it needs systemd, which `/etc/wsl.conf`
   turns on. A Windows task runs only while the user is logged on, and it sees only the user
   account's `PATH`, not the one of a shell: himalaya, carillon, claude and cc-connect must all be
   on it, or the service cannot start them. A service started before `PATH` was changed has to
   be ended and run again.
3. `carillon` is `down`: the listener cannot run. `mx install-tool carillon` if it is missing; the
   reason is in `watch.log`.
4. `queued_alerts` is above 0: the chat cannot be reached (see above). The alerts are sent when it
   can.
5. `problems` lists mailboxes that cannot sign in: see above.
6. Everything is fine and still no alert: alerts only go out for mail in the `now` tier. Run
   `mx look <n>` on the mail to see its tier, and `mx rules` for the rules. A mail is alerted once,
   and mail that arrived while alerts were off is not alerted.

### The log

`watch.log` is in the mail state directory, outside the hub, one folder per hub:

| System | State directory |
|---|---|
| Linux, WSL | `~/.local/state/cc-hub-mail/<id>` (under `$XDG_STATE_HOME` when that is set) |
| macOS | `~/Library/Application Support/cc-hub-mail/<id>` |
| Windows | `%LOCALAPPDATA%\cc-hub-mail\<id>` |

`<id>` is a folder of 12 letters and digits; when there are several, take the one whose
`watch.log` is the newest. Read the last lines of `watch.log` and nothing else in that directory.
It holds carillon's output and the watcher's own lines.

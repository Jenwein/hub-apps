# Remove the ledger app

Follow this before `cc-hub app remove ledger --yes`. Each step checks first, so
a part that is already gone is skipped. The scheduled tasks are unregistered by
the `hub-apps` skill, not here.

## 1. Stop Fava

Check the platform with `uname -s`.

- **Linux.** If `~/.config/systemd/user/cc-hub-ledger-fava.service` exists, run
  `systemctl --user disable --now cc-hub-ledger-fava`, delete the file, and run
  `systemctl --user daemon-reload`. If `systemctl --user` cannot be reached,
  delete the file only.
- **macOS.** If `~/Library/LaunchAgents/io.cc-hub.ledger.fava.plist` exists, run
  `launchctl bootout gui/$(id -u)/io.cc-hub.ledger.fava` (it is fine if that
  says the service is not loaded), then delete the plist.
- **Windows.** Stop the Fava you started for the ledger, if one is running.

Do not turn off lingering (`loginctl disable-linger`): cc-connect may need it.

## 2. Tailscale

If `[fava] tailscale` in `ledger/config.toml` is `true`, run
`tailscale serve status`. If it shows `/` on https port 443 proxying to
`http://127.0.0.1:<port>` (`<port>` is `[fava] port`), run
`tailscale serve --https=443 off`. Leave anything else it serves alone, and
never run `tailscale serve reset`. If Fava is not in the list, or `tailscale` is
not installed, there is nothing to undo.

## 3. The software

The `hub-apps` skill has you ask whether to uninstall the software the app
used. If the user says yes, tell them what each command does and run it only
once they agree:

- `uv tool uninstall fava`, which also removes `bean-check`, `bean-query`,
  `bean-price` and `bean-format`.
- `double-entry-generator`: `rm ~/.local/bin/double-entry-generator` (Linux),
  `brew uninstall double-entry-generator` (macOS), or delete
  `double-entry-generator.exe` from `~/.local/bin` (Windows).

Leave `uv`: the hub needs it. Ask first whether the user uses beancount or Fava
for anything else.

## 4. Tell the user

The ledger stays in `ledger/` in the hub, with its history in git: removing the
app does not touch it. They can read it with any beancount tool. If they
install the app again, `ledger/` is used as it is.

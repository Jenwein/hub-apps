# Fava

Fava shows the ledger as web pages. It listens on `127.0.0.1` only, on the
port `[fava] port` in `ledger/config.toml` (default 5000). It reloads the
ledger by itself when the files change.

## Run it

- Linux (a systemd user service): `systemctl --user status cc-hub-ledger-fava`;
  `systemctl --user restart cc-hub-ledger-fava`; logs with
  `journalctl --user -u cc-hub-ledger-fava -n 50`.
- macOS (a launchd agent): `launchctl print gui/$(id -u)/io.cc-hub.ledger.fava`;
  restart with `launchctl kickstart -k gui/$(id -u)/io.cc-hub.ledger.fava`.
- Windows: no service. When the user wants Fava, start it in the background,
  `fava -H 127.0.0.1 -p <port> ledger/main.beancount`, and stop it when they
  are done.

After a port change, write the service definition again as
`apps/ledger/SETUP.md` does
(`uv run apps/ledger/scripts/lk.py fava-unit --platform linux --ledger <absolute path of ledger/main.beancount>`,
or `--platform macos`), then restart the service.

## Open it

- On a computer: `ssh -L <port>:localhost:<port> <server>`, then open
  `http://localhost:<port>`. On the machine itself, open that address directly.
- On a phone, when `[fava] tailscale` is true: Fava is served inside the
  user's tailnet by `tailscale serve`; `tailscale serve status` shows the
  address. The phone needs Tailscale, logged in to the same account.

## Edits made in Fava

Entries added in Fava go to `ledger/txns/web.beancount`; edits change the file
the entry is in. Fava does not commit them: the next ledger task does, before
its own changes, as `ledger: edits made outside the chat` (SKILL.md, "Writing
to the ledger"). Fava will not save over a file that changed on disk since it
loaded the page; the user reloads the page and edits again.

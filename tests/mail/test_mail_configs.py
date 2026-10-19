import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from mailkit import vault
from mailkit.cli import main
from mailkit.configs import (
    carillon_config,
    credentials,
    himalaya_config,
    hook_command,
    oauth_token_command,
    ortie_config,
    server_url,
    write_configs,
)
from mailkit.context import load
from mailkit.settings import MailError, Server
from mailkit.tools import NO_WINDOW
from mailkit.vault import fetch_command, mx_command, store, token_path
from mailtest import write_mailboxes

POSIX = os.name == "posix"

FOUR = """\
[[mailbox]]
name = "feishu"
address = "me@corp.cn"
display_name = "Me Corp"
provider = "feishu"

[[mailbox]]
name = "qq"
address = "me@qq.com"
provider = "qq"

[[mailbox]]
name = "gmail"
address = "me@gmail.com"
provider = "gmail"

[[mailbox]]
name = "outlook"
address = "me@outlook.com"
provider = "outlook"
"""

PASSWORD_ONLY = """\
[[mailbox]]
name = "qq"
address = "me@qq.com"
provider = "qq"
"""

OUTLOOK_ONLY = """\
[[mailbox]]
name = "outlook"
address = "me@outlook.com"
provider = "outlook"
"""


@pytest.fixture
def four(hub):
    write_mailboxes(hub, FOUR)
    return hub


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def state_toml(hub, name: str) -> dict:
    return tomllib.loads((hub.state / name).read_text("utf-8"))


def terminal(monkeypatch, *typed: str) -> None:
    """Make stdin a terminal; input() answers the typed lines."""
    monkeypatch.setattr(vault, "at_terminal", lambda: True)
    lines = iter(typed)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))


def without_ortie(monkeypatch) -> None:
    """No ortie on PATH, whatever this machine has installed."""
    real = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda cmd, *a, **kw: None if cmd == "ortie" else real(cmd, *a, **kw))


# --- server_url ---


@pytest.mark.parametrize(("scheme", "tls", "url", "starttls"), [
    ("imap", "tls", "imaps://mail.example.com:993", False),
    ("imap", "starttls", "imap://mail.example.com:993", True),
    ("imap", "none", "imap://mail.example.com:993", False),
    ("smtp", "tls", "smtps://mail.example.com:993", False),
    ("smtp", "starttls", "smtp://mail.example.com:993", True),
    ("smtp", "none", "smtp://mail.example.com:993", False),
])
def test_server_url(scheme, tls, url, starttls):
    assert server_url(scheme, Server("mail.example.com", 993, tls)) == (url, starttls)


# --- himalaya.toml ---


def test_himalaya_config_per_provider(four):
    config = himalaya_config(load(four.root))
    accounts = config["accounts"]
    assert list(accounts) == ["feishu", "qq", "gmail", "outlook"]
    assert accounts["qq"]["imap"]["server"] == "imaps://imap.qq.com:993"
    assert accounts["qq"]["smtp"]["server"] == "smtps://smtp.qq.com:465"
    assert accounts["feishu"]["imap"]["server"] == "imaps://imap.feishu.cn:993"
    assert accounts["gmail"]["smtp"]["server"] == "smtps://smtp.gmail.com:465"
    assert accounts["outlook"]["imap"]["server"] == "imaps://outlook.office365.com:993"
    assert accounts["outlook"]["smtp"]["server"] == "smtp://smtp-mail.outlook.com:587"
    assert accounts["outlook"]["smtp"]["starttls"] is True
    assert "starttls" not in accounts["outlook"]["imap"]
    assert "starttls" not in accounts["qq"]["imap"]
    assert accounts["qq"]["imap"]["id"] == {
        "auto": True,
        "fields": {"name": True, "version": True, "vendor": True, "support-url": True}}
    assert "id" not in accounts["feishu"]["imap"] and "id" not in accounts["gmail"]["imap"]
    assert "id" not in accounts["qq"]["smtp"]  # IMAP ID is IMAP's
    xoauth2 = accounts["outlook"]["imap"]["sasl"]["xoauth2"]
    assert xoauth2["username"] == "me@outlook.com"
    assert xoauth2["token"]["command"][-3:] == ["token", "show", "--auto-refresh"]
    assert accounts["outlook"]["smtp"]["sasl"] == accounts["outlook"]["imap"]["sasl"]
    for account in accounts.values():
        assert "message" not in account  # sent copies are appended by mx, not by himalaya


def test_himalaya_account_basics(four):
    accounts = himalaya_config(load(four.root))["accounts"]
    assert accounts["feishu"]["default"] is True
    assert all("default" not in accounts[name] for name in ("qq", "gmail", "outlook"))
    assert accounts["feishu"]["email"] == "me@corp.cn"
    assert accounts["feishu"]["display-name"] == "Me Corp"
    assert "display-name" not in accounts["qq"]  # none given: the key is left out


def test_password_mailboxes_fetch_their_password_through_mx(four):
    ctx = load(four.root)
    accounts = himalaya_config(ctx)["accounts"]
    for name in ("feishu", "qq", "gmail"):
        plain = accounts[name]["imap"]["sasl"]["plain"]
        assert plain == {
            "username": ctx.mailbox(name).address,
            "password": {"command": fetch_command(ctx, name)}}
        assert accounts[name]["smtp"]["sasl"] == accounts[name]["imap"]["sasl"]
    assert credentials(ctx, ctx.mailbox("qq")) == accounts["qq"]["imap"]["sasl"]


def test_cleartext_server_for_tests(hub):
    write_mailboxes(hub, """\
[[mailbox]]
name = "test"
address = "me@localhost"
provider = "feishu"
imap_host = "localhost"
imap_port = 3143
imap_tls = "none"
smtp_host = "localhost"
smtp_port = 3025
smtp_tls = "none"

[[mailbox]]
name = "plain"
address = "me@example.org"
provider = "feishu"
imap_host = "mail.example.org"
imap_port = 143
imap_tls = "starttls"
""")
    accounts = himalaya_config(load(hub.root))["accounts"]
    assert accounts["test"]["imap"]["server"] == "imap://localhost:3143"
    assert "starttls" not in accounts["test"]["imap"]
    assert accounts["test"]["smtp"]["server"] == "smtp://localhost:3025"
    assert "starttls" not in accounts["test"]["smtp"]
    assert accounts["plain"]["imap"]["server"] == "imap://mail.example.org:143"
    assert accounts["plain"]["imap"]["starttls"] is True


def test_folder_aliases_come_from_mailboxes_toml(hub):
    write_mailboxes(hub, """\
[[mailbox]]
name = "qq"
address = "me@qq.com"
provider = "qq"

[mailbox.folders]
sent = "Sent Messages"
trash = "Deleted Messages"
archive = ""

[[mailbox]]
name = "bare"
address = "me@feishu.cn"
provider = "feishu"
""")
    accounts = himalaya_config(load(hub.root))["accounts"]
    assert accounts["qq"]["mailbox"]["alias"] == {
        "inbox": "INBOX", "sent": "Sent Messages", "trash": "Deleted Messages"}
    assert accounts["bare"]["mailbox"]["alias"] == {"inbox": "INBOX"}  # unknown roles: no alias


# --- carillon.toml ---


def test_carillon_watches_inboxes_and_hooks_mx(four):
    ctx = load(four.root)
    accounts = carillon_config(ctx)["accounts"]
    assert list(accounts) == ["feishu", "qq", "gmail", "outlook"]
    imap = accounts["qq"]["imap"]
    assert imap["mailbox"] == "INBOX"
    assert imap["server"] == "imaps://imap.qq.com:993"
    assert imap["id"]["auto"] is True
    assert imap["sasl"] == himalaya_config(ctx)["accounts"]["qq"]["imap"]["sasl"]
    assert imap["hook"]["on-message-added"]["cmd"] == mx_command(ctx, "hook", "qq")
    assert hook_command(ctx, "qq") == mx_command(ctx, "hook", "qq")
    assert "smtp" not in accounts["qq"]
    assert accounts["outlook"]["imap"]["sasl"]["xoauth2"]["token"]["command"][-3:] == [
        "token", "show", "--auto-refresh"]
    assert accounts["outlook"]["imap"]["hook"]["on-message-added"]["cmd"][-2:] == [
        "hook", "outlook"]


def test_carillon_watches_the_inbox_the_mailbox_names(hub):
    write_mailboxes(hub, """\
[[mailbox]]
name = "a"
address = "a@feishu.cn"
provider = "feishu"
[mailbox.folders]
inbox = "Posteingang"

[[mailbox]]
name = "b"
address = "b@feishu.cn"
provider = "feishu"
[mailbox.folders]
inbox = ""
""")
    accounts = carillon_config(load(hub.root))["accounts"]
    assert accounts["a"]["imap"]["mailbox"] == "Posteingang"
    assert accounts["b"]["imap"]["mailbox"] == "INBOX"  # carillon needs one


# --- ortie.toml ---


def test_ortie_config_only_with_an_outlook_mailbox(hub):
    write_mailboxes(hub, PASSWORD_ONLY)
    assert write_configs(load(hub.root)) == ["himalaya.toml", "carillon.toml"]
    assert not (hub.state / "ortie.toml").exists()

    write_mailboxes(hub, FOUR)
    ctx = load(hub.root)
    assert write_configs(ctx) == ["himalaya.toml", "carillon.toml", "ortie.toml"]
    accounts = state_toml(hub, "ortie.toml")["accounts"]
    assert list(accounts) == ["outlook"]  # password mailboxes have no token
    assert accounts["outlook"] == {
        "client-id": "9e5f94bc-e8a4-4e73-b8be-63364c29d753",
        "endpoints": {
            "authorization": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
            "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            "device-authorization":
                "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode",
            "redirection": "https://localhost",
        },
        "scopes": [
            "https://outlook.office.com/IMAP.AccessAsUser.All",
            "https://outlook.office.com/SMTP.Send",
            "offline_access",
        ],
        "grant": "device",
        "auto-refresh": True,
        "storage": {
            "read": {"command": mx_command(ctx, "token-store", "read", "outlook")},
            "write": {"command": mx_command(ctx, "token-store", "write", "outlook")},
        },
    }
    assert ortie_config(ctx) == {"accounts": accounts}


def test_ortie_grant_comes_from_oauth_json(hub):
    write_mailboxes(hub, OUTLOOK_ONLY + """
[[mailbox]]
name = "second"
address = "me@hotmail.com"
provider = "outlook"
""")
    ctx = load(hub.root)
    (hub.state / "oauth.json").write_text(
        json.dumps({"second": "authorization-code", "outlook": "nonsense"}), "utf-8")
    accounts = ortie_config(ctx)["accounts"]
    assert accounts["second"]["grant"] == "authorization-code"
    assert accounts["outlook"]["grant"] == "device"  # a value it does not know: the default


def test_token_command_is_the_absolute_ortie_when_installed(hub, make_exe):
    write_mailboxes(hub, OUTLOOK_ONLY)
    make_exe("ortie", "")
    command = oauth_token_command(load(hub.root), "outlook")
    assert command[1:] == ["-c", "ortie.toml", "-a", "outlook", "token", "show", "--auto-refresh"]
    assert os.path.isabs(command[0])
    assert command[0] == shutil.which("ortie")


def test_token_command_works_before_ortie_is_installed(hub, monkeypatch):
    write_mailboxes(hub, OUTLOOK_ONLY)
    without_ortie(monkeypatch)
    assert oauth_token_command(load(hub.root), "outlook") == [
        "ortie", "-c", "ortie.toml", "-a", "outlook", "token", "show", "--auto-refresh"]
    assert write_configs(load(hub.root)) == ["himalaya.toml", "carillon.toml", "ortie.toml"]


def test_no_config_names_a_config_by_an_absolute_path(four):
    ctx = load(four.root)
    write_configs(ctx)

    def commands(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "command" or key == "cmd":
                    yield value
                else:
                    yield from commands(value)

    seen = 0
    for name in ("himalaya.toml", "carillon.toml", "ortie.toml"):
        for argv in commands(state_toml(four, name)):
            seen += 1
            for flag in ("-c", "--config"):
                if flag in argv:
                    assert argv[argv.index(flag) + 1] == "ortie.toml"
    assert seen >= 8


# --- write_configs ---


def test_no_secret_in_any_generated_file(four):
    store(four.state, "qq", "S3CRET")
    vault.store_token(four.state, "outlook", b'{"access_token": "T0KEN"}')
    assert main(["configure"]) == 0
    files = [p for p in four.state.rglob("*") if p.is_file() and "secrets" not in p.parts]
    assert {p.name for p in files} >= {"himalaya.toml", "carillon.toml", "ortie.toml"}
    for path in files:
        text = path.read_text("utf-8")
        assert "S3CRET" not in text and "T0KEN" not in text, path
    assert (four.state / "secrets" / "qq").read_text("utf-8") == "S3CRET"  # it was there to leak


def test_configure_is_idempotent(four, capsys):
    assert main(["configure"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["written"] == ["himalaya.toml", "carillon.toml", "ortie.toml"]
    for name in first["written"]:
        assert tomllib.loads((four.state / name).read_text("utf-8"))  # TOML, not empty
        if POSIX:
            assert mode(four.state / name) == 0o600
    before = {n: (four.state / n).stat().st_mtime_ns for n in first["written"]}

    assert main(["configure"]) == 0
    assert json.loads(capsys.readouterr().out)["written"] == []
    assert {n: (four.state / n).stat().st_mtime_ns for n in first["written"]} == before

    # A change that only himalaya.toml shows is the only file rewritten.
    write_mailboxes(four, FOUR.replace('name = "qq"\n', 'name = "qq"\ndisplay_name = "Q"\n'))
    assert main(["configure"]) == 0
    assert json.loads(capsys.readouterr().out)["written"] == ["himalaya.toml"]


def test_configure_rewrites_a_file_that_was_edited_by_hand(four, capsys):
    assert main(["configure"]) == 0
    capsys.readouterr()
    (four.state / "carillon.toml").write_text("# edited\n", "utf-8")
    assert main(["configure"]) == 0
    assert json.loads(capsys.readouterr().out)["written"] == ["carillon.toml"]


def test_configure_lists_missing_secrets_and_sign_ins(four, capsys):
    assert main(["configure"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["missing_secrets"] == ["feishu", "qq", "gmail"]
    assert out["missing_sign_in"] == ["outlook"]

    store(four.state, "qq", "x")
    store(four.state, "gmail", "x")
    vault.store_token(four.state, "outlook", b"{}")
    assert main(["configure"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"written": [], "missing_secrets": ["feishu"], "missing_sign_in": []}


def test_configure_needs_a_hub(tmp_path, capsys):
    assert main(["--hub", str(tmp_path), "configure"]) == 1
    assert "no mail/mailboxes.toml" in capsys.readouterr().err


# --- token-store ---


def feed(monkeypatch, data: bytes) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data), encoding="utf-8"))


def test_token_store_round_trip_and_0600(four, monkeypatch, capsys):
    token = '{"access_token": "abc", "refresh_token": "def", "issued_at": 1}'
    feed(monkeypatch, token.encode())
    assert main(["token-store", "write", "outlook"]) == 0
    assert capsys.readouterr().out == ""
    path = token_path(four.state, "outlook")
    assert path == four.state / "secrets" / "outlook.oauth.json"
    assert path.read_text("utf-8") == token
    if POSIX:
        assert mode(path) == 0o600
        assert mode(path.parent) == 0o700

    assert main(["token-store", "read", "outlook"]) == 0
    assert capsys.readouterr().out == token

    feed(monkeypatch, b'{"access_token": "new"}')  # a refresh replaces it whole
    assert main(["token-store", "write", "outlook"]) == 0
    capsys.readouterr()
    assert main(["token-store", "read", "outlook"]) == 0
    assert capsys.readouterr().out == '{"access_token": "new"}'
    assert sorted(p.name for p in path.parent.iterdir()) == ["outlook.oauth.json"]


def test_token_store_read_without_a_token_fails(four, capsys):
    assert main(["token-store", "read", "outlook"]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "no token stored for outlook" in out.err and "oauth-login outlook" in out.err


@pytest.mark.parametrize("data", [b"", b"not json", b"{\"access_token\": \"S3CRET", b"\xff\xfe"])
def test_token_store_write_of_something_else_than_json_fails(four, monkeypatch, capsys, data):
    vault.store_token(four.state, "outlook", b'{"kept": true}')
    feed(monkeypatch, data)
    assert main(["token-store", "write", "outlook"]) == 1
    err = capsys.readouterr().err
    assert "not JSON" in err and "S3CRET" not in err
    assert token_path(four.state, "outlook").read_text("utf-8") == '{"kept": true}'


def test_token_store_is_for_oauth_mailboxes_only(four, monkeypatch, capsys):
    feed(monkeypatch, b"{}")
    assert main(["token-store", "write", "qq"]) == 1
    assert "password" in capsys.readouterr().err
    assert main(["token-store", "read", "nope"]) == 1
    assert "no mailbox nope" in capsys.readouterr().err
    assert main(["token-store", "read", "../x"]) == 1
    assert not (four.state / "secrets").exists()


def test_the_token_commands_in_ortie_toml_round_trip_through_mx(four):
    """Run the generated storage commands for real, with python in the place of `uv run`."""
    ctx = load(four.root)
    write_configs(ctx)
    storage = state_toml(four, "ortie.toml")["accounts"]["outlook"]["storage"]
    env = {**os.environ, "CC_HUB_MAIL_STATE": str(four.state)}

    def run(command, data=None):
        assert command[:2] == [shutil.which("uv"), "run"]
        argv = [sys.executable, *command[2:]]
        return subprocess.run(
            argv, input=data, capture_output=True, env=env, check=False, timeout=60,
            creationflags=NO_WINDOW)

    token = b'{"access_token": "abc"}'
    assert run(storage["write"]["command"], token).returncode == 0
    done = run(storage["read"]["command"])
    assert (done.returncode, done.stdout) == (0, token)


# --- oauth-login ---


def test_oauth_login_refuses_password_mailboxes_and_pipes(four, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("whatever\n"))  # a pipe
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked"))
    assert main(["oauth-login", "outlook"]) == 1
    assert capsys.readouterr().err == (
        "mx: type it yourself in the server's terminal: "
        "uv run apps/mail/scripts/mx.py oauth-login outlook\n")
    assert not (four.state / "ortie.toml").exists()

    terminal(monkeypatch)
    assert main(["oauth-login", "qq"]) == 1
    err = capsys.readouterr().err
    assert "qq signs in with a password" in err and "secret-set qq" in err

    assert main(["oauth-login", "nope"]) == 1
    assert "no mailbox nope" in capsys.readouterr().err
    assert not (four.state / "ortie.toml").exists()


def test_oauth_login_needs_ortie(four, monkeypatch, capsys):
    terminal(monkeypatch)
    without_ortie(monkeypatch)
    assert main(["oauth-login", "outlook"]) == 1
    err = capsys.readouterr().err
    assert "ortie is not installed" in err and "mx.py install-tool ortie" in err
    assert not (four.state / "ortie.toml").exists()


# A fake ortie: it logs what it was called with and where, reads the config it was given by the
# relative name (so a wrong working directory fails), and answers like ortie v2.3.0.
FAKE_ORTIE = '''
import json
import os
import sys
import tomllib
from pathlib import Path

argv = sys.argv[1:]
config = argv[argv.index("-c") + 1]
account = argv[argv.index("-a") + 1]
with Path(__file__).with_name("ortie.log").open("a", encoding="utf-8") as log:
    log.write(json.dumps({
        "argv": argv, "cwd": os.getcwd(), "config_here": Path(config).is_file()}) + "\\n")
grant = tomllib.loads(Path(config).read_text("utf-8"))["accounts"][account]["grant"]
rest = argv[argv.index("-a") + 2:]
fail = os.environ.get("FAKE_ORTIE_FAIL", "")


def say(data, code=0):
    print(json.dumps(data))
    sys.exit(code)


if rest[-2:] == ["auth", "get"]:
    if fail == "get":
        say({"error": "unauthorized_client: the client may not use the device grant"}, 1)
    if grant == "device":
        say({"device_code": "DEVCODE123", "user_code": "ABCD-1234",
             "verification_uri": "https://microsoft.com/devicelogin",
             "verification_uri_complete": "https://microsoft.com/devicelogin?otc=ABCD-1234",
             "expires_in": 900, "interval": 5, "interactive": False})
    say({"authorization_uri": "https://login.example/authorize?client_id=x&state=STATE42",
         "state": "STATE42", "pkce_code_verifier": "VERIFIER99", "interactive": False})
elif "resume" in rest:
    if fail == "resume":
        print("Error: access_denied")
        sys.exit(1)
elif rest[-2:] == ["token", "inspect"]:
    if fail == "inspect":
        say({"error": "no token in the storage"}, 1)
    say({"token_type": "Bearer", "issued_at": 1, "expires_in": 3600,
         "with_refresh_token": True, "scope": "x"})
else:
    sys.exit(2)
'''


def ortie_calls(exe: Path) -> list[dict]:
    log = exe.parent / "ortie.log"
    return [json.loads(line) for line in log.read_text("utf-8").splitlines()]


def in_state(call: dict, hub) -> bool:
    return os.path.samefile(call["cwd"], hub.state)


def test_oauth_login_device_flow(four, make_exe, monkeypatch, capsys):
    exe = make_exe("ortie", FAKE_ORTIE)
    terminal(monkeypatch)
    assert main(["oauth-login", "outlook"]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out) == {"signed_in": "outlook"}
    # The user reads the code and the address on the terminal and opens them on any device.
    assert "ABCD-1234" in out.err and "https://microsoft.com/devicelogin" in out.err
    assert "DEVCODE123" not in out.out + out.err

    calls = ortie_calls(exe)
    base = ["-c", "ortie.toml", "-a", "outlook"]
    assert [c["argv"] for c in calls] == [
        [*base, "--json", "auth", "get"],
        [*base, "auth", "resume", "DEVCODE123"],
        [*base, "--json", "token", "inspect"],
    ]
    assert all(in_state(c, four) and c["config_here"] for c in calls)
    assert state_toml(four, "ortie.toml")["accounts"]["outlook"]["grant"] == "device"
    assert json.loads((four.state / "oauth.json").read_text("utf-8")) == {"outlook": "device"}
    assert {"himalaya.toml", "carillon.toml", "ortie.toml"} <= {
        p.name for p in four.state.iterdir()}


def test_oauth_login_paste_flow(four, make_exe, monkeypatch, capsys):
    exe = make_exe("ortie", FAKE_ORTIE)
    redirected = "https://localhost/?code=CODE123"
    terminal(monkeypatch, f"  {redirected}  ")
    assert main(["oauth-login", "outlook", "--paste"]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out) == {"signed_in": "outlook"}
    assert "https://login.example/authorize?client_id=x&state=STATE42" in out.err
    assert "VERIFIER99" not in out.out + out.err

    base = ["-c", "ortie.toml", "-a", "outlook"]
    calls = ortie_calls(exe)
    assert [c["argv"] for c in calls] == [
        [*base, "--json", "auth", "get"],
        [*base, "auth", "resume", "--state", "STATE42", "--pkce", "VERIFIER99", redirected],
        [*base, "--json", "token", "inspect"],
    ]
    assert all(in_state(c, four) and c["config_here"] for c in calls)
    assert state_toml(four, "ortie.toml")["accounts"]["outlook"]["grant"] == "authorization-code"
    assert json.loads((four.state / "oauth.json").read_text("utf-8")) == {
        "outlook": "authorization-code"}

    # The next sign-in without --paste goes back to the device grant, and the config follows.
    terminal(monkeypatch)
    assert main(["oauth-login", "outlook"]) == 0
    assert state_toml(four, "ortie.toml")["accounts"]["outlook"]["grant"] == "device"


@pytest.mark.parametrize("where", ["get", "resume"])
def test_a_refused_device_grant_says_to_use_paste(four, make_exe, monkeypatch, capsys, where):
    exe = make_exe("ortie", FAKE_ORTIE)
    monkeypatch.setenv("FAKE_ORTIE_FAIL", where)
    terminal(monkeypatch)
    assert main(["oauth-login", "outlook"]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "oauth-login outlook --paste" in out.err
    assert [c["argv"][-1] for c in ortie_calls(exe)] == (
        ["get"] if where == "get" else ["get", "DEVCODE123"])


def test_a_refused_code_sign_in_is_an_error_without_the_device_advice(
        four, make_exe, monkeypatch, capsys):
    make_exe("ortie", FAKE_ORTIE)
    monkeypatch.setenv("FAKE_ORTIE_FAIL", "resume")
    terminal(monkeypatch, "https://localhost/?error=access_denied")
    assert main(["oauth-login", "outlook", "--paste"]) == 1
    message = capsys.readouterr().err.rsplit("mx: ", 1)[-1]  # after the instructions
    assert message == "ortie could not finish the sign-in (exit 1)\n"


def test_sign_in_that_stored_no_token_is_not_a_sign_in(four, make_exe, monkeypatch, capsys):
    make_exe("ortie", FAKE_ORTIE)
    monkeypatch.setenv("FAKE_ORTIE_FAIL", "inspect")
    terminal(monkeypatch)
    assert main(["oauth-login", "outlook"]) == 1
    out = capsys.readouterr()
    assert out.out == "" and "no token" in out.err


@pytest.mark.parametrize("typed", ["", "   ", "-x"])
def test_paste_flow_needs_an_address(four, make_exe, monkeypatch, capsys, typed):
    exe = make_exe("ortie", FAKE_ORTIE)
    terminal(monkeypatch, typed)
    assert main(["oauth-login", "outlook", "--paste"]) == 1
    assert "not an address" in capsys.readouterr().err
    assert len(ortie_calls(exe)) == 1  # only `auth get`


def test_paste_flow_cancelled_at_the_prompt(four, make_exe, monkeypatch, capsys):
    exe = make_exe("ortie", FAKE_ORTIE)
    monkeypatch.setattr(vault, "at_terminal", lambda: True)

    def interrupted(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupted)
    assert main(["oauth-login", "outlook", "--paste"]) == 1
    assert "cancelled" in capsys.readouterr().err
    assert len(ortie_calls(exe)) == 1


def test_paste_flow_with_no_input_at_all(four, make_exe, monkeypatch, capsys):
    make_exe("ortie", FAKE_ORTIE)
    monkeypatch.setattr(vault, "at_terminal", lambda: True)

    def closed(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)
    assert main(["oauth-login", "outlook", "--paste"]) == 1
    assert "nothing signed in" in capsys.readouterr().err


def test_ctrl_c_while_ortie_waits_cancels_the_sign_in(four, make_exe, monkeypatch, capsys):
    make_exe("ortie", FAKE_ORTIE)
    terminal(monkeypatch)
    real_run = subprocess.run

    def run(argv, **kwargs):
        if not kwargs.get("capture_output"):  # the terminal-attached call: the user gives up
            raise KeyboardInterrupt
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    assert main(["oauth-login", "outlook"]) == 1
    out = capsys.readouterr()
    assert out.out == "" and "mx: cancelled: nothing signed in" in out.err


def test_configure_with_no_mailbox_writes_empty_configs(hub, capsys):
    assert main(["configure"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "written": ["himalaya.toml", "carillon.toml"], "missing_secrets": [],
        "missing_sign_in": []}
    assert state_toml(hub, "himalaya.toml") == {"accounts": {}}


def test_a_token_file_is_named_after_a_valid_mailbox_only(tmp_path):
    assert token_path(tmp_path, "outlook") == tmp_path / "secrets" / "outlook.oauth.json"
    for name in ("../x", "a/b", "A", ""):
        with pytest.raises(MailError, match="not a mailbox name"):
            token_path(tmp_path, name)

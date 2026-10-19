"""The `mx` command line: one subparser per command, each with a `func(args) -> int | None`."""

from __future__ import annotations

import argparse
import getpass
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

from . import configs, context, tools, vault
from .paths import locked
from .settings import Mailbox, MailError
from .tools import NO_WINDOW


class Parser(argparse.ArgumentParser):
    """A usage error is `mx: <message>` and exit 1, like any other error; the usage follows the
    message. The subparsers are of this class too."""

    def error(self, message: str) -> NoReturn:
        raise MailError(f"{message}\n{self.format_usage().rstrip()}")


def print_json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def load_ctx(args: argparse.Namespace) -> context.Ctx:
    """The context of the hub named by --hub. Commands that need a hub call this themselves:
    some (install-tool) must work where there is none."""
    return context.load(args.hub)


def run_install_tool(args: argparse.Namespace) -> None:
    """Needs no hub: the pinned tools are installed where there is none yet."""
    dest = Path(args.dir).expanduser().absolute() if args.dir else Path.home() / ".local" / "bin"
    path = tools.install_tool(args.tool, dest)
    print_json({"installed": str(path), "version": tools.TOOLS[args.tool].version})


def password_mailbox(ctx: context.Ctx, name: str) -> Mailbox:
    """The mailbox `name`, which must sign in with a password: an OAuth one has a token instead."""
    box = ctx.mailbox(name)
    if box.auth != "password":
        raise MailError(f"{name} signs in with OAuth, it has no password: mx oauth-login {name}")
    return box


def run_secret_set(args: argparse.Namespace) -> None:
    """Only a person at a terminal can set a secret: it is typed, never passed as an argument,
    a pipe or a file, so it cannot come from the chat or from the agent."""
    if not vault.at_terminal():
        raise MailError(
            "type it yourself in the server's terminal: "
            f"uv run apps/mail/scripts/mx.py secret-set {args.mailbox}")
    ctx = load_ctx(args)
    box = password_mailbox(ctx, args.mailbox)
    try:
        first = getpass.getpass(f"Password of {box.name} ({box.address}): ")
        again = getpass.getpass("Again: ")
    except (EOFError, KeyboardInterrupt):
        raise MailError("cancelled: nothing stored") from None
    if first != again:
        raise MailError("the two entries differ: nothing stored")
    vault.store(ctx.state, box.name, first)
    print_json({"stored": box.name})


def run_secret_delete(args: argparse.Namespace) -> None:
    ctx = load_ctx(args)
    box = ctx.mailbox(args.mailbox)
    print_json({"deleted": box.name, "existed": vault.remove(ctx.state, box.name)})


def run_secret_status(args: argparse.Namespace) -> None:
    ctx = load_ctx(args)
    print_json({
        name: "oauth" if box.auth == "oauth" else vault.exists(ctx.state, name)
        for name, box in ctx.mailboxes.items()})


def run_secret_fetch(args: argparse.Namespace) -> None:
    """The password command of himalaya and carillon: the secret itself, with no line break and
    nothing else on stdout. The only command that prints a password."""
    ctx = load_ctx(args)
    box = password_mailbox(ctx, args.mailbox)
    secret = vault.read(ctx.state, box.name)
    if secret is None:
        raise MailError(
            f"no password stored for {box.name}: "
            f"type it in the server's terminal with mx secret-set {box.name}")
    sys.stdout.write(secret)
    sys.stdout.flush()


def run_configure(args: argparse.Namespace) -> None:
    """Write the himalaya, carillon and ortie configs, and say what the user still has to type."""
    ctx = load_ctx(args)
    written = configs.write_configs(ctx)
    boxes = ctx.mailboxes.items()
    print_json({
        "written": written,
        "missing_secrets": [
            name for name, box in boxes
            if box.auth == "password" and not vault.exists(ctx.state, name)],
        "missing_sign_in": [
            name for name, box in boxes
            if box.auth == "oauth" and not vault.has_token(ctx.state, name)],
    })


def oauth_mailbox(ctx: context.Ctx, name: str) -> Mailbox:
    """The mailbox `name`, which must sign in with OAuth: a password one has no token."""
    box = ctx.mailbox(name)
    if box.auth != "oauth":
        raise MailError(f"{name} signs in with a password, not OAuth: mx secret-set {name}")
    return box


def read_stdin_bytes() -> bytes:
    stream = sys.stdin
    if stream is None:
        return b""
    buffer = getattr(stream, "buffer", None)
    return buffer.read() if buffer is not None else stream.read().encode("utf-8")


def run_token_store(args: argparse.Namespace) -> None:
    """The storage commands of ortie: `read` prints the token it asks for, `write` keeps what it
    sends on stdin. The only commands that handle a token, and only ortie calls them."""
    ctx = load_ctx(args)
    box = oauth_mailbox(ctx, args.mailbox)
    if args.action == "write":
        vault.store_token(ctx.state, box.name, read_stdin_bytes())
        return
    token = vault.read_token(ctx.state, box.name)
    if token is None:
        raise MailError(
            f"no token stored for {box.name}: "
            f"sign in on the server's terminal with mx oauth-login {box.name}")
    sys.stdout.write(token)
    sys.stdout.flush()


def ortie_json(ctx: context.Ctx, ortie: str, mailbox: str, *args: str) -> dict:
    """Run ortie in the state directory and read its JSON answer. A failure is a MailError with
    the error ortie gives."""
    try:
        done = subprocess.run(
            configs.ortie_argv(ortie, mailbox, "--json", *args), cwd=ctx.state, check=False,
            stdin=subprocess.DEVNULL, capture_output=True, timeout=60, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise MailError(f"cannot run ortie: {err}") from err
    try:
        answer = json.loads(done.stdout)
    except ValueError:  # not JSON, or bytes that are not UTF-8
        answer = None
    if done.returncode != 0:
        said = answer.get("error") if isinstance(answer, dict) else None
        reason = said[:300] if isinstance(said, str) and said else f"exit {done.returncode}"
        raise MailError(f"ortie said: {reason}")
    if not isinstance(answer, dict):
        raise MailError("ortie answered something that is not JSON")
    return answer


def ortie_attached(ctx: context.Ctx, ortie: str, mailbox: str, *args: str) -> None:
    """Run ortie on the terminal the user is at: it waits for the user, and shows its progress."""
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        done = subprocess.run(  # no capture, no NO_WINDOW: this one talks to the person
            configs.ortie_argv(ortie, mailbox, *args), cwd=ctx.state, check=False)
    except OSError as err:
        raise MailError(f"cannot run ortie: {err}") from err
    if done.returncode != 0:
        raise MailError(f"ortie could not finish the sign-in (exit {done.returncode})")


def answer_field(answer: dict, key: str) -> str:
    value = answer.get(key)
    if not isinstance(value, str) or not value:
        raise MailError(f"ortie's answer has no {key}")
    return value


def sign_in_with_device(ctx: context.Ctx, ortie: str, name: str) -> None:
    """The device grant: the user opens an address on any device and types a short code there."""
    try:
        answer = ortie_json(ctx, ortie, name, "auth", "get")
        device_code = answer_field(answer, "device_code")
        user_code = answer_field(answer, "user_code")
        address = answer_field(answer, "verification_uri")
        print(f"On any device, open {address} and enter the code {user_code}", file=sys.stderr)
        print("Waiting until you have approved the sign-in...", file=sys.stderr, flush=True)
        ortie_attached(ctx, ortie, name, "auth", "resume", device_code)
    except MailError as err:
        raise MailError(
            f"{err}\nIf Microsoft refuses the device sign-in, run it again with --paste: "
            f"uv run apps/mail/scripts/mx.py oauth-login {name} --paste") from None


def sign_in_with_code(ctx: context.Ctx, ortie: str, name: str) -> None:
    """The authorization-code grant: the browser ends on https://localhost/?code=..., which does
    not load, and the user pastes that address here."""
    answer = ortie_json(ctx, ortie, name, "auth", "get")
    address = answer_field(answer, "authorization_uri")
    state = answer_field(answer, "state")
    verifier = answer_field(answer, "pkce_code_verifier")
    print(f"Open this address in a browser and sign in:\n{address}\n", file=sys.stderr)
    print(
        "The browser ends on a page that does not load, https://localhost/?code=...\n"
        "Copy its address from the address bar and paste it here: ",
        end="", file=sys.stderr, flush=True)
    try:
        pasted = input().strip()
    except EOFError:
        raise MailError("no address given: nothing signed in") from None
    if not pasted or pasted.startswith("-"):
        raise MailError("that is not an address: nothing signed in")
    ortie_attached(
        ctx, ortie, name, "auth", "resume", "--state", state, "--pkce", verifier, pasted)


def sign_in(ctx: context.Ctx, ortie: str, name: str, *, paste: bool) -> None:
    """Run the sign-in, then ask ortie whether it left a token."""
    if paste:
        sign_in_with_code(ctx, ortie, name)
    else:
        sign_in_with_device(ctx, ortie, name)
    try:
        ortie_json(ctx, ortie, name, "token", "inspect")
    except MailError as err:
        raise MailError(f"the sign-in left no token for {name}: {err}") from None


def run_oauth_login(args: argparse.Namespace) -> None:
    """Sign an Outlook mailbox in. Like secret-set it needs a person at a terminal: the code or
    the pasted address is the user's consent, and it cannot come from the chat."""
    if not vault.at_terminal():
        raise MailError(
            "type it yourself in the server's terminal: "
            f"uv run apps/mail/scripts/mx.py oauth-login {args.mailbox}")
    ctx = load_ctx(args)
    box = oauth_mailbox(ctx, args.mailbox)
    ortie = shutil.which("ortie")
    if ortie is None:
        raise MailError("ortie is not installed: uv run apps/mail/scripts/mx.py install-tool ortie")
    with locked(ctx.state):
        configs.record_grant(ctx.state, box.name, "authorization-code" if args.paste else "device")
        configs.write_configs(ctx)
    try:
        sign_in(ctx, ortie, box.name, paste=args.paste)
    except KeyboardInterrupt:
        raise MailError("cancelled: nothing signed in") from None
    print_json({"signed_in": box.name})


def run_forget(args: argparse.Namespace) -> None:
    ctx = load_ctx(args)
    with locked(ctx.state):
        removed = vault.forget(ctx.state, secrets=args.secrets)
    print_json({"removed": removed, "secrets_kept": (ctx.state / "secrets").exists()})


def build_parser() -> Parser:
    parser = Parser(prog="mx", description="Tools for the mail app.")
    parser.add_argument(
        "--hub", type=Path, default=Path(), metavar="DIR",
        help="the hub, the directory with mail/mailboxes.toml (default: the current directory)")
    # Name the result `sub` and add a command as
    #     p = sub.add_parser("name", help="..."); p.set_defaults(func=run_name)
    # run_name(args) returns the exit status; None means 0. It calls load_ctx(args) if it needs
    # the hub.
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    p = sub.add_parser(
        "install-tool", help="download a pinned tool from its official release and check it")
    p.add_argument("tool", choices=list(tools.TOOLS))
    p.add_argument(
        "--dir", metavar="DIR",
        help="where to put the binary (default: .local/bin in your home directory)")
    p.set_defaults(func=run_install_tool)

    p = sub.add_parser(
        "secret-set", help="store the password of a mailbox (type it yourself, in a terminal)")
    p.add_argument("mailbox")
    p.set_defaults(func=run_secret_set)

    p = sub.add_parser("secret-delete", help="delete the stored password of a mailbox")
    p.add_argument("mailbox")
    p.set_defaults(func=run_secret_delete)

    p = sub.add_parser(
        "secret-status", help="for each mailbox: is a password stored (never shows it)")
    p.set_defaults(func=run_secret_status)

    # No help text, so --help does not list it: only himalaya and carillon call it.
    p = sub.add_parser("secret-fetch")
    p.add_argument("mailbox")
    p.set_defaults(func=run_secret_fetch)

    p = sub.add_parser(
        "configure", help="write the himalaya, carillon and ortie configs from mailboxes.toml")
    p.set_defaults(func=run_configure)

    p = sub.add_parser(
        "oauth-login", help="sign an Outlook mailbox in (do it yourself, in a terminal)")
    p.add_argument("mailbox")
    p.add_argument(
        "--paste", action="store_true",
        help="sign in by pasting the address the browser ends on, if the device sign-in is refused")
    p.set_defaults(func=run_oauth_login)

    # No help text, like secret-fetch: only ortie calls it.
    p = sub.add_parser("token-store")
    p.add_argument("action", choices=["read", "write"])
    p.add_argument("mailbox")
    p.set_defaults(func=run_token_store)

    p = sub.add_parser(
        "forget", help="delete this machine's mail state: drafts, marks, generated configs")
    p.add_argument(
        "--secrets", action="store_true", help="delete the stored passwords too (kept otherwise)")
    p.set_defaults(func=run_forget)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return args.func(args) or 0
    except MailError as err:
        print(f"mx: {err}", file=sys.stderr)
        return 1

"""The `mx` command line: one subparser per command, each with a `func(args) -> int | None`."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import NoReturn

from . import context, tools, vault
from .paths import locked
from .settings import Mailbox, MailError


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

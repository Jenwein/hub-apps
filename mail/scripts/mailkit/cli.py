"""The `mk` command line: one subparser per command, each with a `func(args) -> int | None`."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import NoReturn

from . import accounts, config
from .config import MailError
from .providers import PROVIDERS


class Parser(argparse.ArgumentParser):
    """A usage error is `mk: <message>` and exit 1, like any other error. The subparsers are of
    this class too."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        raise MailError(message)


def print_json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def run_config_check(args: argparse.Namespace) -> None:
    settings = config.load(args.mail_dir)
    print_json({"ok": True, "mailboxes": list(settings.mailboxes)})


def password_from_stdin() -> str:
    """The password piped to stdin (one line; the line ending goes). Never an argument, so it
    never shows in a process list or a shell history. A terminal is asked without echo."""
    stream = sys.stdin
    if stream is None:
        raise MailError("pipe the password to stdin")
    text = getpass.getpass("password: ") if stream.isatty() else stream.read()
    password = text.removeprefix("\ufeff").strip("\r\n")
    if not password:
        raise MailError(
            "no password on stdin: pipe the password into the command (it is never taken as "
            "an argument)")
    return password


def print_result(result: dict) -> int:
    """Print a result with an `ok` key; a failed one exits with 1."""
    print_json(result)
    return 0 if result.get("ok", True) else 1


def run_account_detect(args: argparse.Namespace) -> int:
    return print_result(accounts.detect(args.address))


def run_account_add(args: argparse.Namespace) -> int:
    password = password_from_stdin()
    return print_result(accounts.add(
        args.hub, args.mail_dir, args.name, args.address, password, provider=args.provider))


def run_account_test(args: argparse.Namespace) -> int:
    return print_result(accounts.test(args.hub, args.mail_dir, args.name))


def run_account_edit(args: argparse.Namespace) -> int:
    if args.rename is None and not args.password_stdin and args.paused is None:
        raise MailError("nothing to change: give --rename, --password-stdin, --pause or --resume")
    password = password_from_stdin() if args.password_stdin else None
    return print_result(accounts.edit(
        args.hub, args.mail_dir, args.name, new_name=args.rename, password=password,
        paused=args.paused))


def run_account_remove(args: argparse.Namespace) -> int:
    return print_result(accounts.remove(args.hub, args.mail_dir, args.name))


def run_account_list(args: argparse.Namespace) -> int:
    return print_result(accounts.list_(args.hub, args.mail_dir, page=args.page))


def add_account_commands(sub) -> None:
    p = sub.add_parser("account", help="add, check, change, remove and list mailboxes")
    actions = p.add_subparsers(dest="action", metavar="<action>", required=True)

    a = actions.add_parser(
        "detect", help="which supported provider an address belongs to, and its guide")
    a.add_argument("address")
    a.set_defaults(func=run_account_detect)

    a = actions.add_parser(
        "add", help="add a mailbox: log in to its IMAP and SMTP servers, then store it; the "
        "password is read from stdin")
    a.add_argument("name", help='the short name, such as "工作" or "QQ"')
    a.add_argument("--address", required=True, help="the email address")
    a.add_argument("--provider", choices=list(PROVIDERS),
                   help="the provider, when the address does not tell it")
    a.set_defaults(func=run_account_add)

    a = actions.add_parser("test", help="log in to both servers with the stored password")
    a.add_argument("name")
    a.set_defaults(func=run_account_test)

    a = actions.add_parser("edit", help="rename, change the password, pause or resume")
    a.add_argument("name")
    a.add_argument("--rename", metavar="NEW_NAME", help="a new short name")
    a.add_argument("--password-stdin", action="store_true",
                   help="read a new password from stdin; it is stored after both logins work")
    paused = a.add_mutually_exclusive_group()
    paused.add_argument("--pause", dest="paused", action="store_const", const=True,
                        help="its mail goes to the digest only, never pushed")
    paused.add_argument("--resume", dest="paused", action="store_const", const=False,
                        help="push important mail again")
    a.set_defaults(func=run_account_edit)

    a = actions.add_parser(
        "remove", help="stop managing a mailbox: its section, password and index go; "
        "downloaded attachments stay")
    a.add_argument("name")
    a.set_defaults(func=run_account_remove)

    a = actions.add_parser("list", help="the mailboxes, 10 to a page")
    a.add_argument("--page", type=int, default=1)
    a.set_defaults(func=run_account_list)


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(prog="mk", description="Tools for the mail app.")
    parser.add_argument(
        "--mail-dir", type=Path, default=Path("mail"), metavar="DIR",
        help="the mail data directory, the one with config.toml (default: mail, in the "
        "working directory, which is the hub)")
    parser.add_argument(
        "--hub", type=Path, default=Path("."), metavar="DIR",
        help="the hub directory, where secrets/ lives (default: the working directory)")
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    # Add a command as
    #     p = sub.add_parser("name", help="..."); p.set_defaults(func=run_name)
    # run_name(args) returns the exit status; None means 0. The global options come before it:
    #     mk.py --mail-dir DIR name
    p = sub.add_parser("config", help="the settings in config.toml")
    actions = p.add_subparsers(dest="action", metavar="<action>", required=True)
    check = actions.add_parser(
        "check", help="read config.toml and print the mailboxes in it; an error names the bad key")
    check.set_defaults(func=run_config_check)
    add_account_commands(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return args.func(args) or 0
    except MailError as err:
        print(f"mk: {err}", file=sys.stderr)
        return 1

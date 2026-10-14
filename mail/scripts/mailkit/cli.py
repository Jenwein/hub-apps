"""The `mk` command line: one subparser per command, each with a `func(args) -> int | None`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NoReturn

from . import config
from .config import MailError


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
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return args.func(args) or 0
    except MailError as err:
        print(f"mk: {err}", file=sys.stderr)
        return 1

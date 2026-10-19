"""The `mx` command line: one subparser per command, each with a `func(args) -> int | None`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NoReturn

from . import context
from .settings import MailError


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


def build_parser() -> Parser:
    parser = Parser(prog="mx", description="Tools for the mail app.")
    parser.add_argument(
        "--hub", type=Path, default=Path(), metavar="DIR",
        help="the hub, the directory with mail/mailboxes.toml (default: the current directory)")
    # Name the result `sub` and add a command as
    #     p = sub.add_parser("name", help="..."); p.set_defaults(func=run_name)
    # run_name(args) returns the exit status; None means 0. It calls load_ctx(args) if it needs
    # the hub.
    parser.add_subparsers(dest="command", metavar="<command>", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return args.func(args) or 0
    except MailError as err:
        print(f"mx: {err}", file=sys.stderr)
        return 1

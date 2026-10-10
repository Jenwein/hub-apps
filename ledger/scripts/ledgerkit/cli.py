"""The `lk` command line: one subparser per command, each with a `func(args) -> int | None`."""

from __future__ import annotations

import argparse
import sys

from .settings import LedgerError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lk", description="Tools for the ledger app.")
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)  # noqa: F841
    # Add a command as
    #     p = sub.add_parser("name", help="..."); p.set_defaults(func=run_name)
    # run_name(args) returns the exit status; None means 0.
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except LedgerError as err:
        print(f"lk: {err}", file=sys.stderr)
        return 1

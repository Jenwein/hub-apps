"""The `lk` command line: one subparser per command, each with a `func(args) -> int | None`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import book
from .settings import LedgerError


def print_json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def add_ledger_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ledger-dir", type=Path, required=True, metavar="DIR",
        help="the ledger directory (the one with main.beancount)")


def run_add(args: argparse.Namespace) -> None:
    try:
        text = sys.stdin.buffer.read().decode("utf-8-sig")
    except UnicodeDecodeError as err:
        raise LedgerError(f"the entries on stdin must be UTF-8 text: {err}") from err
    print_json(book.add_text(args.ledger_dir, text))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lk", description="Tools for the ledger app.")
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    # Add a command as
    #     p = sub.add_parser("name", help="..."); add_ledger_dir(p); p.set_defaults(func=run_name)
    # run_name(args) returns the exit status; None means 0.
    p = sub.add_parser("add", help="write beancount entries read from stdin to the ledger")
    add_ledger_dir(p)
    p.set_defaults(func=run_add)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except LedgerError as err:
        print(f"lk: {err}", file=sys.stderr)
        return 1

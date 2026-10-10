"""The `lk` command line: one subparser per command, each with a `func(args) -> int | None`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import book, convert, match
from .settings import LedgerError, load_settings, source

# The default rules of the app: <app>/importers, next to <app>/scripts.
DEFAULTS_DIR = Path(__file__).resolve().parents[2] / "importers"


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


def run_convert(args: argparse.Namespace) -> None:
    settings = load_settings(args.ledger_dir)
    print_json(convert.convert(
        source(settings, args.source), args.file, args.out, settings=settings,
        defaults_dir=DEFAULTS_DIR, ledger_dir=args.ledger_dir, account=args.account))


def run_match(args: argparse.Namespace) -> None:
    settings = load_settings(args.ledger_dir)
    print_json(match.run(args.ledger_dir / "main.beancount", args.staged, args.out, settings))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lk", description="Tools for the ledger app.")
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    # Add a command as
    #     p = sub.add_parser("name", help="..."); add_ledger_dir(p); p.set_defaults(func=run_name)
    # run_name(args) returns the exit status; None means 0.
    p = sub.add_parser("add", help="write beancount entries read from stdin to the ledger")
    add_ledger_dir(p)
    p.set_defaults(func=run_add)
    p = sub.add_parser(
        "convert", help="convert a bill to staged beancount entries with double-entry-generator")
    add_ledger_dir(p)
    p.add_argument("--source", required=True, help="the bill source, e.g. alipay, wechat, ccb")
    p.add_argument("--file", type=Path, required=True, metavar="BILL", help="the bill as exported")
    p.add_argument("--out", type=Path, required=True, metavar="STAGED",
                   help="the staged beancount file to write")
    p.add_argument("--account", help="the account of the card (required for bank statements)")
    p.set_defaults(func=run_convert)
    p = sub.add_parser(
        "match", help="sort staged entries into new, duplicate, fix and uncertain; write a plan")
    add_ledger_dir(p)
    p.add_argument("--staged", type=Path, required=True, metavar="STAGED",
                   help="the staged beancount file, e.g. from convert")
    p.add_argument("--out", type=Path, required=True, metavar="PLAN",
                   help="the plan file to write (JSON), for apply")
    p.set_defaults(func=run_match)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except LedgerError as err:
        print(f"lk: {err}", file=sys.stderr)
        return 1

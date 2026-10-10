"""The `lk` command line: one subparser per command, each with a `func(args) -> int | None`."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from . import apply, book, convert, match, table
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


def parse_decisions(pairs: list[str]) -> dict[str, str]:
    """`ID=new|skip` arguments as a dict.

    A bad one is a LedgerError (exit 1) here: an argparse error would exit 2, which means that
    apply needs decisions. The same decision twice is harmless, two different ones are an error.
    """
    decisions = {}
    for pair in pairs:
        id_, sep, choice = pair.partition("=")
        if not sep or not id_:
            raise LedgerError(f"--decide takes ID=new or ID=skip, got {pair!r}")
        if decisions.setdefault(id_, choice) != choice:
            raise LedgerError(f"two different decisions for {id_}")
    return decisions


def run_apply(args: argparse.Namespace) -> int:
    decisions = parse_decisions(args.decide)
    try:
        result = apply.apply_plan(
            args.plan, args.ledger_dir, decisions, args.document, args.document_account)
    except apply.NeedsDecision as err:
        print_json(err.payload)
        return 2
    print_json(result)
    return 0


def run_table(args: argparse.Namespace) -> None:
    rows = table.read_table(args.file, sheet=args.sheet, password=args.password)
    csv.writer(sys.stdout, lineterminator="\n").writerows(rows)


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
    p = sub.add_parser(
        "apply", help="write a plan from match to the ledger; exit 2 when it needs decisions")
    add_ledger_dir(p)
    p.add_argument("--plan", type=Path, required=True, metavar="PLAN",
                   help="the plan file written by match")
    p.add_argument("--decide", action="append", default=[], metavar="ID=new|skip",
                   help="what to do with an uncertain item: write it (new) or drop it (skip)")
    p.add_argument("--document", type=Path, metavar="FILE",
                   help="the bill to file under the ledger's documents")
    p.add_argument("--document-account", metavar="ACCOUNT",
                   help="the account the document belongs to (an open account)")
    p.set_defaults(func=run_apply)
    p = sub.add_parser(
        "table", help="print a bill (csv, txt, xlsx, xls or pdf) as CSV text for the agent to read")
    p.add_argument("file", type=Path, metavar="FILE", help="the bill as exported")
    p.add_argument("--sheet", type=int, default=0, metavar="N",
                   help="the sheet of a spreadsheet to print, counting from 0 (default 0)")
    p.add_argument("--password", metavar="P",
                   help="the password of an encrypted PDF; used for this read only")
    p.set_defaults(func=run_table)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except LedgerError as err:
        print(f"lk: {err}", file=sys.stderr)
        return 1

"""The `mk` command line: one subparser per command, each with a `func(args) -> int | None`."""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import NoReturn

from . import accounts, background, config, poll
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


def number(text: str, minimum: int) -> int:
    """An argument that is a plain decimal number (no sign, no spaces, no underscores) from
    `minimum` up to what the index can hold."""
    if not re.fullmatch(r"[0-9]+", text):
        raise argparse.ArgumentTypeError(f"{text!r} is not a number")
    value = int(text)
    if value < minimum:
        raise argparse.ArgumentTypeError(f"must be at least {minimum}")
    if value > background.MAX_NUMBER:
        raise argparse.ArgumentTypeError(f"{text} is too large")
    return value


def positive_int(text: str) -> int:
    return number(text, 1)


def non_negative_int(text: str) -> int:
    return number(text, 0)


def run_poll(args: argparse.Namespace) -> None:
    wake = (lambda: poll.wake_triage(args.hub)) if args.wake else None
    print_json(poll.poll(args.hub, args.mail_dir, wake=wake))


def add_poll_command(sub) -> None:
    """`mk poll`: the poll that wakes `triage`. It is outside `bg` on purpose: the restricted
    tasks may run only `mk bg ...`, so they cannot wake themselves."""
    p = sub.add_parser(
        "poll", help="take new mail from every mailbox's INBOX into the index without "
        "changing anything on the server, route it, and note mailbox problems; the poll task "
        "runs this")
    p.add_argument("--wake", action="store_true",
                   help="wake the mail/triage task (cc-hub cron wake) when there is mail to "
                   "judge or a mailbox problem to report")
    p.set_defaults(func=run_poll)


def run_bg_poll(args: argparse.Namespace) -> None:
    print_json(poll.poll(args.hub, args.mail_dir))


def run_bg_pending(args: argparse.Namespace) -> None:
    print_json(background.pending(args.mail_dir, page=args.page))


def run_bg_judge(args: argparse.Namespace) -> None:
    print_json(background.judge(args.mail_dir, args.decisions))


def run_bg_read(args: argparse.Namespace) -> None:
    print_json(background.read(args.mail_dir, args.id))


def run_bg_search(args: argparse.Namespace) -> None:
    print_json(background.search(
        args.hub, args.mail_dir, args.text, mailbox=args.mailbox, sender=args.sender,
        since=args.since, page=args.page, on_server=args.server))


def run_bg_recent_pushed(args: argparse.Namespace) -> None:
    print_json(background.recent_pushed(args.mail_dir, limit=args.limit))


def run_bg_problems(args: argparse.Namespace) -> None:
    print_json(background.problems(
        args.mail_dir, mark_reported=args.mark_reported, page=args.page))


def run_bg_digest_items(args: argparse.Namespace) -> None:
    print_json(background.digest_items(args.mail_dir, page=args.page, up_to=args.up_to))


def run_bg_digest_done(args: argparse.Namespace) -> None:
    print_json(background.digest_done(args.mail_dir, args.up_to))


def run_bg_status(args: argparse.Namespace) -> None:
    print_json(background.status(args.mail_dir))


def add_bg_commands(sub) -> None:
    """`mk bg ...`: the background commands. They never change anything on a mail server
    (no send, flag, move or delete), never print a password, and take only ids, page numbers,
    `ID=DECISION` pairs and short words: the restricted triage and digest tasks may run only
    these, and the command line of a restricted task cannot hold shell syntax."""
    p = sub.add_parser(
        "bg", help="background commands: they only read mail and the local index; the "
        "restricted triage and digest tasks may run only these")
    actions = p.add_subparsers(dest="action", metavar="<action>", required=True)

    a = actions.add_parser(
        "poll", help="take new mail from every mailbox's INBOX into the index without "
        "changing anything on the server (it never wakes triage; `mk poll --wake` does)")
    a.set_defaults(func=run_bg_poll)

    a = actions.add_parser(
        "pending", help="the mail waiting for a verdict, 10 to a page, oldest first, with the "
        "rules of each mailbox and the mailbox problems not reported yet; a judged message "
        "leaves the list, so after judging a page ask for page 1 again")
    a.add_argument("--page", type=positive_int, default=1)
    a.set_defaults(func=run_bg_pending)

    a = actions.add_parser(
        "judge", help="give verdicts to pending mail: ID=important (push it) or ID=digest "
        "(leave it for the daily digest), for example: judge 12=important 13=digest")
    a.add_argument("decisions", nargs="+", metavar="ID=DECISION")
    a.set_defaults(func=run_bg_judge)

    a = actions.add_parser("read", help="one stored message, its whole text included")
    a.add_argument("id", type=non_negative_int, help="the id of the message")
    a.set_defaults(func=run_bg_read)

    a = actions.add_parser(
        "search", help="find mail in the local index, 10 to a page, newest first; with "
        "--server in each mailbox's own search instead, for mail older than the index")
    a.add_argument("text", nargs="?", default="",
                   help="one substring of the subject, the sender or the text, as one "
                   "argument (empty: any)")
    a.add_argument("--mailbox", metavar="NAME", help="only this mailbox, by its short name")
    a.add_argument("--from", dest="sender", metavar="ADDR",
                   help="only mail whose From address or name contains this")
    a.add_argument("--since", metavar="YYYY-MM-DD", help="only mail from that day on")
    a.add_argument("--page", type=positive_int, default=1)
    a.add_argument("--server", action="store_true",
                   help="ask the mail servers (read-only); the hits are not stored and have "
                   "no id; needs the search text")
    a.set_defaults(func=run_bg_search)

    a = actions.add_parser(
        "recent-pushed", help="the last pushes, newest first: what a reply to that one "
        "points at")
    a.add_argument("--limit", type=positive_int, default=None,
                   help=f"how many (default {background.PUSHED_DEFAULT}, "
                   f"at most {background.PUSHED_MAX})")
    a.set_defaults(func=run_bg_recent_pushed)

    a = actions.add_parser(
        "problems", help="the mailbox problems and recoveries not reported yet, 10 to a page")
    a.add_argument("--page", type=positive_int, default=1)
    a.add_argument("--mark-reported", nargs="+", type=positive_int, metavar="ID",
                   help="the user was told about the problems with these ids (the `ids` of "
                   "an item), instead of listing")
    a.set_defaults(func=run_bg_problems)

    a = actions.add_parser(
        "digest-items", help="the mail waiting for the digest, 10 to a page, grouped by "
        "mailbox, with the pushes since the last digest, every open problem and whether "
        "polling stopped; page 1 returns up_to, later pages must pass it back")
    a.add_argument("--page", type=positive_int, default=1)
    a.add_argument("--up-to", type=non_negative_int, metavar="N",
                   help="the up_to of the first page")
    a.set_defaults(func=run_bg_digest_items)

    a = actions.add_parser(
        "digest-done", help="the digest was sent: the mail listed up to N becomes digested "
        "(N is the up_to of digest-items; 0 when it listed nothing)")
    a.add_argument("up_to", type=non_negative_int, metavar="UP_TO")
    a.set_defaults(func=run_bg_digest_done)

    a = actions.add_parser(
        "status", help="when the last poll ran, whether that is too long ago, and the state "
        "of each mailbox")
    a.set_defaults(func=run_bg_status)


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
    add_poll_command(sub)
    add_bg_commands(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return args.func(args) or 0
    except MailError as err:
        print(f"mk: {err}", file=sys.stderr)
        return 1
    except sqlite3.Error as err:  # the index is damaged, or busy in a way Store did not name
        print(f"mk: the mail index failed: {err}", file=sys.stderr)
        return 1

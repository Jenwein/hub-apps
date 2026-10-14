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

from . import accounts, background, config, drafts, poll
from . import actions as mail_actions
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
    """Print a result with an `ok` key; a failed one exits with 1. (A command that changes mail
    is `ok` only when every id was done; the JSON says which were not, and why.)"""
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
    """`mk poll --wake` is the poll task: only that run counts for `bg status`."""
    wake = (lambda: poll.wake_triage(args.hub)) if args.wake else None
    print_json(poll.poll(args.hub, args.mail_dir, wake=wake, background=args.wake))


def add_poll_command(sub) -> None:
    """`mk poll`: the poll that wakes `triage`. It is outside `bg` on purpose: the restricted
    tasks may run only `mk bg ...`, so they cannot wake themselves."""
    p = sub.add_parser(
        "poll", help="take new mail from every mailbox's INBOX into the index without "
        "changing anything on the server, route it, and note mailbox problems; the poll task "
        "runs this")
    p.add_argument("--wake", action="store_true",
                   help="wake the mail/triage task (cc-hub cron wake) when there is mail to "
                   "judge or a mailbox problem to report; the poll task runs this, and only "
                   "this run counts as the background check in `bg status`")
    p.set_defaults(func=run_poll)


def run_bg_poll(args: argparse.Namespace) -> None:
    """Polls, but is no run of the background check: `bg status` stays as it was."""
    print_json(poll.poll(args.hub, args.mail_dir, background=False))


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


def run_attachment_get(args: argparse.Namespace) -> None:
    """`attachment get` and `bg attachment`: the same function, so what the restricted tasks may
    run is exactly what the agent runs."""
    path = mail_actions.download_attachment(args.hub, args.mail_dir, args.id, args.index)
    print_json({"path": path.as_posix()})


def add_bg_commands(sub) -> None:
    """`mk bg ...`: the background commands. They never change anything on a mail server
    (no send, flag, move or delete; `attachment` only reads, with BODY.PEEK, and writes a file
    on this machine), never print a password, and take only ids, page numbers, `ID=DECISION`
    pairs and short words: the restricted triage and digest tasks may run only these, and the
    command line of a restricted task cannot hold shell syntax."""
    p = sub.add_parser(
        "bg", help="background commands: they only read mail and the local index; the "
        "restricted triage and digest tasks may run only these")
    actions = p.add_subparsers(dest="action", metavar="<action>", required=True)

    a = actions.add_parser(
        "poll", help="take new mail from every mailbox's INBOX into the index without "
        "changing anything on the server (it never wakes triage, and `bg status` does not "
        "count it as a run of the background check; `mk poll --wake` does both)")
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
        "status", help="when the poll task (mk poll --wake) last ran, whether that is too "
        "long ago, and the state of each mailbox")
    a.set_defaults(func=run_bg_status)

    a = actions.add_parser(
        "attachment", help="save one attachment of a message on this machine and print its "
        "path; the message stays unread. Same as `attachment get`")
    add_attachment_arguments(a)
    a.set_defaults(func=run_attachment_get)


def add_attachment_arguments(parser) -> None:
    parser.add_argument("id", type=non_negative_int, help="the id of the message")
    parser.add_argument("index", type=non_negative_int,
                        help="which attachment, counting from 0 (the `index` in the message's "
                        "attachment list)")


def run_flag(args: argparse.Namespace) -> int:
    return print_result(mail_actions.flag(args.hub, args.mail_dir, args.ids, args.seen))


def run_archive(args: argparse.Namespace) -> int:
    return print_result(
        mail_actions.archive(args.hub, args.mail_dir, args.ids, create=args.create))


def run_delete_list(args: argparse.Namespace) -> None:
    if args.show is not None:
        if args.ids:
            raise MailError("give either message ids or --show LIST_ID, not both")
        print_json(mail_actions.show_delete_list(args.mail_dir, args.show, page=args.page or 1))
        return
    if args.page is not None:
        raise MailError("--page goes with --show LIST_ID")
    if not args.ids:
        raise MailError("give message ids to list, or --show LIST_ID to see a list again")
    print_json(mail_actions.delete_list(args.mail_dir, args.ids))


def run_delete(args: argparse.Namespace) -> int:
    return print_result(mail_actions.delete(args.hub, args.mail_dir, args.list_id))


def add_mail_commands(sub) -> None:
    """The commands that change mail on a server, or save an attachment, when the user asks.
    They are outside `bg` on purpose: the restricted tasks cannot run them."""
    p = sub.add_parser(
        "flag", help="mark messages read (--seen) or unread (--unseen) on the server; no "
        "confirmation needed. Example: mk flag 12 13 --seen")
    p.add_argument("ids", nargs="+", type=non_negative_int, metavar="ID",
                   help="message ids, as `bg search` lists them")
    mark = p.add_mutually_exclusive_group(required=True)
    mark.add_argument("--seen", dest="seen", action="store_const", const=True,
                      help="mark them read")
    mark.add_argument("--unseen", dest="seen", action="store_const", const=False,
                      help="mark them unread")
    p.set_defaults(func=run_flag)

    p = sub.add_parser(
        "archive", help="move messages out of INBOX to the archive folder (Gmail: All Mail); "
        "no confirmation needed. Example: mk archive 12 13")
    p.add_argument("ids", nargs="+", type=non_negative_int, metavar="ID",
                   help="message ids, as `bg search` lists them")
    p.add_argument("--create", action="store_true",
                   help="when a mailbox has no archive folder: create one named Archive (or use "
                   "a folder of that name) after the user agreed")
    p.set_defaults(func=run_archive)

    p = sub.add_parser(
        "delete-list", help="step 1 of deleting: save the exact set of messages to delete, "
        "print the first 10 and the total, and show them to the user; nothing is deleted. "
        "Example: mk delete-list 12 13 14. To page through a list again: "
        "mk delete-list --show LIST_ID --page 2")
    p.add_argument("ids", nargs="*", type=non_negative_int, metavar="ID",
                   help="message ids, as `bg search` lists them")
    p.add_argument("--show", metavar="LIST_ID", help="show a saved list instead of making one")
    p.add_argument("--page", type=positive_int, default=None, help="with --show: which page")
    p.set_defaults(func=run_delete_list)

    p = sub.add_parser(
        "delete", help="step 2 of deleting, only after the user said to delete what "
        "delete-list showed: move exactly those messages to Trash (nothing is erased) and "
        "forget the list. Example: mk delete k3j9x2ab")
    p.add_argument("list_id", metavar="LIST_ID", help="the list_id that delete-list printed")
    p.set_defaults(func=run_delete)

    p = sub.add_parser("attachment", help="save an attachment of a message on this machine")
    attachment_actions = p.add_subparsers(dest="action", metavar="<action>", required=True)
    a = attachment_actions.add_parser(
        "get", help="save one attachment and print its path (mail/attachments/<address>/"
        "<YYYY-MM>/<id>-<name>); the message stays unread. Example: mk attachment get 12 0")
    add_attachment_arguments(a)
    a.set_defaults(func=run_attachment_get)


def read_body(path: Path, hub: Path) -> str:
    """The text of a mail from the file the agent wrote: UTF-8 (a byte order mark is dropped
    by drafts). A file of the hub's secrets, or a .env file, is refused."""
    drafts.refuse_secret_file(path, hub, "the body file")
    try:
        data = Path(path).read_bytes()
    except OSError as err:
        raise MailError(f"cannot read the body file {path}: {err.strerror or err}") from None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise MailError(f"the body file {path} is not UTF-8 text; write it as UTF-8") from None


def run_draft_new(args: argparse.Namespace) -> None:
    print_json(drafts.create(
        args.hub, args.mail_dir, mailbox=args.mailbox, to=args.to, cc=args.cc,
        subject=args.subject, body=read_body(args.body_file, args.hub), attach=args.attach))


def run_draft_reply(args: argparse.Namespace) -> None:
    print_json(drafts.create(
        args.hub, args.mail_dir, mailbox=args.mailbox, to=args.to, cc=args.cc,
        subject=args.subject, body=read_body(args.body_file, args.hub), attach=args.attach,
        reply_to=args.id, reply_all=args.all))


def run_draft_forward(args: argparse.Namespace) -> None:
    print_json(drafts.create(
        args.hub, args.mail_dir, mailbox=args.mailbox, to=args.to, cc=args.cc,
        subject=args.subject, body=read_body(args.body_file, args.hub), attach=args.attach,
        forward_of=args.id))


def run_draft_edit(args: argparse.Namespace) -> None:
    print_json(drafts.edit(
        args.mail_dir, args.draft_id, hub=args.hub, mailbox=args.mailbox, to=args.to,
        cc=[] if args.no_cc else args.cc, subject=args.subject,
        body=None if args.body_file is None else read_body(args.body_file, args.hub),
        attach=args.attach, drop=args.drop))


def run_draft_show(args: argparse.Namespace) -> None:
    print(drafts.show(args.mail_dir, args.draft_id))


def run_send(args: argparse.Namespace) -> int:
    return print_result(drafts.send(args.hub, args.mail_dir, args.draft_id))


def add_draft_options(parser, *, to_required: bool = False, extra: bool = False) -> None:
    """--from, --to, --cc, --subject, --body-file and --attach of draft new, reply and forward.
    `extra`: --to and --cc add to the recipients a reply already has."""
    parser.add_argument("--from", dest="mailbox", metavar="NAME",
                        help="the mailbox to send from, by its short name")
    added = " (added to the reply's own)" if extra else ""
    parser.add_argument("--to", action="append", required=to_required,
                        default=None if to_required else [], metavar="ADDR",
                        help=f"a recipient, one address each time; repeat for more{added}")
    parser.add_argument("--cc", action="append", default=[], metavar="ADDR",
                        help=f"a Cc recipient; repeat for more{added}")
    parser.add_argument("--body-file", type=Path, required=True, metavar="FILE",
                        help="a UTF-8 file with the text of the mail, written by the agent")
    parser.add_argument("--attach", action="append", type=Path, default=[], metavar="PATH",
                        help="a file to attach; repeat for more")


def add_draft_commands(sub) -> None:
    """`mk draft ...` writes a draft and shows it; `mk send` sends one. Outside `bg` on
    purpose: the restricted tasks can neither write nor send mail."""
    p = sub.add_parser(
        "draft", help="write mail as a draft: a new mail, a reply or a forward; show it to "
        "the user in full (draft show), and send it with `send` only after they say so")
    actions = p.add_subparsers(dest="action", metavar="<action>", required=True)

    a = actions.add_parser(
        "new", help="a new mail. Example: mk draft new --from 工作 --to boss@corp.example "
        "--subject 周报 --body-file mail/.state/body.txt --attach report.pdf")
    add_draft_options(a, to_required=True)
    a.add_argument("--subject", required=True, help="the subject, one line")
    a.set_defaults(func=run_draft_new)

    a = actions.add_parser(
        "reply", help="a reply to a message, from the mailbox that received it, to its sender "
        "(or Reply-To); with --all also to its To and Cc; never to the user's own addresses. "
        "Example: mk draft reply 12 --body-file mail/.state/body.txt")
    a.add_argument("id", type=non_negative_int, help="the id of the message to answer")
    add_draft_options(a, extra=True)
    a.add_argument("--all", action="store_true", help="reply to all: the To and Cc as well")
    a.add_argument("--subject", help='instead of "Re: <its subject>"')
    a.set_defaults(func=run_draft_reply)

    a = actions.add_parser(
        "forward", help="a forward of a message with its attachments, its text quoted below "
        "yours. Example: mk draft forward 12 --to hr@corp.example --body-file "
        "mail/.state/body.txt")
    a.add_argument("id", type=non_negative_int, help="the id of the message to forward")
    add_draft_options(a, to_required=True)
    a.add_argument("--subject", help='instead of "Fwd: <its subject>"')
    a.set_defaults(func=run_draft_forward)

    a = actions.add_parser(
        "edit", help="change a draft; show it again before sending. Example: mk draft edit "
        "k3j9x2ab --subject 改期 --drop 2")
    a.add_argument("draft_id", metavar="DRAFT_ID")
    a.add_argument("--from", dest="mailbox", metavar="NAME", help="send from this mailbox")
    a.add_argument("--to", action="append", metavar="ADDR",
                   help="the new To list, one address each time (replaces the old one)")
    cc = a.add_mutually_exclusive_group()
    cc.add_argument("--cc", action="append", metavar="ADDR",
                    help="the new Cc list, one address each time (replaces the old one)")
    cc.add_argument("--no-cc", action="store_true", help="no Cc at all")
    a.add_argument("--subject", help="the new subject")
    a.add_argument("--body-file", type=Path, metavar="FILE", help="the new text, a UTF-8 file")
    a.add_argument("--attach", action="append", type=Path, metavar="PATH",
                   help="a file to add; repeat for more")
    a.add_argument("--drop", action="append", type=positive_int, metavar="N",
                   help="remove attachment N, as draft show numbers them; repeat for more")
    a.set_defaults(func=run_draft_edit)

    a = actions.add_parser(
        "show", help="print the whole draft as text (not JSON) to show the user: mailbox, "
        "recipients, subject, attachments with sizes, a warning when it is too large, the "
        "text. Only a draft shown and not changed since can be sent. Example: "
        "mk draft show k3j9x2ab")
    a.add_argument("draft_id", metavar="DRAFT_ID")
    a.set_defaults(func=run_draft_show)

    p = sub.add_parser(
        "send", help="send a draft, only after the user saw all of it (draft show) and said "
        "to send; a draft changed since it was shown is refused. A copy goes to Sent and the "
        "draft is deleted. Example: mk send k3j9x2ab")
    p.add_argument("draft_id", metavar="DRAFT_ID")
    p.set_defaults(func=run_send)


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
    add_mail_commands(sub)
    add_draft_commands(sub)
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

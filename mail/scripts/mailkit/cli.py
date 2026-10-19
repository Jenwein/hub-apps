"""The `mx` command line: one subparser per command, each with a `func(args) -> int | None`."""

from __future__ import annotations

import argparse
import getpass
import json
import re
import shutil
import sys
from contextlib import suppress
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import NoReturn

from . import (
    chat,
    compose,
    configs,
    context,
    himalaya,
    message,
    query,
    reader,
    rules,
    signin,
    strings,
    tools,
    vault,
    watch,
)
from .paths import locked
from .settings import FOLDER_ROLES, Mailbox, MailError


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


def run_configure(args: argparse.Namespace) -> None:
    """Write the himalaya, carillon and ortie configs, and say what the user still has to type."""
    ctx = load_ctx(args)
    written = configs.write_configs(ctx)
    boxes = ctx.mailboxes.items()
    print_json({
        "written": written,
        "missing_secrets": [
            name for name, box in boxes
            if box.auth == "password" and not vault.exists(ctx.state, name)],
        "missing_sign_in": [
            name for name, box in boxes
            if box.auth == "oauth" and not vault.has_token(ctx.state, name)],
    })


def oauth_mailbox(ctx: context.Ctx, name: str) -> Mailbox:
    """The mailbox `name`, which must sign in with OAuth: a password one has no token."""
    box = ctx.mailbox(name)
    if box.auth != "oauth":
        raise MailError(f"{name} signs in with a password, not OAuth: mx secret-set {name}")
    return box


def read_stdin_bytes() -> bytes:
    stream = sys.stdin
    if stream is None:
        return b""
    buffer = getattr(stream, "buffer", None)
    return buffer.read() if buffer is not None else stream.read().encode("utf-8")


def run_token_store(args: argparse.Namespace) -> None:
    """The storage commands of ortie: `read` prints the token it asks for, `write` keeps what it
    sends on stdin. The only commands that handle a token, and only ortie calls them."""
    ctx = load_ctx(args)
    box = oauth_mailbox(ctx, args.mailbox)
    if args.action == "write":
        vault.store_token(ctx.state, box.name, read_stdin_bytes())
        return
    token = vault.read_token(ctx.state, box.name)
    if token is None:
        raise MailError(
            f"no token stored for {box.name}: "
            f"sign in on the server's terminal with mx oauth-login {box.name}")
    sys.stdout.write(token)
    sys.stdout.flush()


def run_oauth_login(args: argparse.Namespace) -> None:
    """Sign an Outlook mailbox in. Like secret-set it needs a person at a terminal: the code or
    the pasted address is the user's consent, and it cannot come from the chat."""
    if not vault.at_terminal():
        raise MailError(
            "type it yourself in the server's terminal: "
            f"uv run apps/mail/scripts/mx.py oauth-login {args.mailbox}")
    ctx = load_ctx(args)
    box = oauth_mailbox(ctx, args.mailbox)
    ortie = shutil.which("ortie")
    if ortie is None:
        raise MailError("ortie is not installed: uv run apps/mail/scripts/mx.py install-tool ortie")
    with locked(ctx.state):
        configs.record_grant(ctx.state, box.name, "authorization-code" if args.paste else "device")
        configs.write_configs(ctx)
    try:
        signin.sign_in(ctx, ortie, box.name, paste=args.paste)
    except KeyboardInterrupt:
        raise MailError("cancelled: nothing signed in") from None
    print_json({"signed_in": box.name})


def run_forget(args: argparse.Namespace) -> None:
    ctx = load_ctx(args)
    with locked(ctx.state):
        removed = vault.forget(ctx.state, secrets=args.secrets)
    print_json({"removed": removed, "secrets_kept": (ctx.state / "secrets").exists()})


def run_folders(args: argparse.Namespace) -> None:
    """The folders of a mailbox as the server names them, and the ones that look like its sent,
    trash and archive folders (what to put under `[mailbox.folders]`)."""
    ctx = load_ctx(args)
    box = ctx.mailbox(args.mailbox)
    found = himalaya.Himalaya(ctx).folders(box.name)
    print_json({
        "folders": [{"name": f.name, "shown": f.shown} for f in found],
        "guess": himalaya.guess_folders(found)})


def check_mailbox(hima: himalaya.Himalaya, box: Mailbox) -> dict:
    """Does the mailbox sign in and answer? Both backends open, then one mail of the inbox is
    listed. A failure is the answer, with its kind (auth, connect or other)."""
    try:
        for backend, ok, error in hima.check(box.name):
            if not ok:
                text = f"{backend}: {error or 'failed'}"
                return {
                    "ok": False, "kind": himalaya.kind(himalaya.classify(error or "")),
                    "error": text[:300]}
        hima.search(box.name, box.folders.get("inbox") or "INBOX", [], 1)
    except himalaya.HimalayaError as err:
        return {"ok": False, "kind": himalaya.kind(type(err)), "error": str(err)}
    return {"ok": True}


def run_check(args: argparse.Namespace) -> None:
    """Test the mailboxes (the named one, else all). Exit 0 whatever the answers are."""
    ctx = load_ctx(args)
    boxes = [ctx.mailbox(args.mailbox)] if args.mailbox else list(ctx.mailboxes.values())
    hima = himalaya.Himalaya(ctx)
    print_json({box.name: check_mailbox(hima, box) for box in boxes})


def day(text: str) -> date:
    """A `--since` or `--before` value: YYYY-MM-DD, and no other spelling of a date."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        with suppress(ValueError):  # such as 2026-13-01
            return date.fromisoformat(text)
    raise argparse.ArgumentTypeError(f"{text!r} is not a date: use YYYY-MM-DD")


def positive_int(text: str) -> int:
    if not (text.isascii() and text.isdigit()) or int(text) < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number from 1 up")
    return int(text)


def run_list(args: argparse.Namespace) -> None:
    """Search the mailboxes and print one numbered list. The numbers are what the other commands
    take, and they mean the last list only."""
    ctx = load_ctx(args)
    listing = query.search(ctx, himalaya.Himalaya(ctx), query.Query(
        mailboxes=tuple(args.mailbox), folder=args.folder, since=args.since, before=args.before,
        sender=args.sender, subject=args.subject, body=args.body, unseen=args.unseen,
        limit=args.limit))
    query.save_listing(ctx, listing)
    print_json({
        "items": [asdict(found) for found in listing.items],
        "errors": listing.errors,
        "note": query.NOTE})


def run_reader_check(args: argparse.Namespace) -> None:
    """Does the reader work? It reads a built-in sample mail with the configured model. Exit 0
    whatever the answer is: a reader that cannot run is the answer `{"ok": false, ...}`."""
    ctx = load_ctx(args)
    print_json(reader.check(reader.Reader(ctx.settings.reader_model), ctx.settings.language))


def run_rules(args: argparse.Namespace) -> None:
    """Plain text, not JSON: the rules as the reader gets them."""
    print(rules.combined(load_ctx(args)))


def run_chat_set(args: argparse.Namespace) -> None:
    """Record the chat this runs in as the private chat, and say so in it."""
    ctx = load_ctx(args)
    recorded = chat.record(ctx)
    chat.send_text(recorded, strings.t("chat_hello", ctx.settings.language))
    print_json({"chat": asdict(recorded)})


def run_chat_show(args: argparse.Namespace) -> None:
    found = chat.saved(load_ctx(args))
    print_json({"chat": asdict(found) if found else None})


def run_show(args: argparse.Namespace) -> None:
    ctx = load_ctx(args)
    print_json(message.show(ctx, himalaya.Himalaya(ctx), args.n))


def run_look(args: argparse.Namespace) -> None:
    ctx = load_ctx(args)
    print_json(message.look(ctx, himalaya.Himalaya(ctx), args.n))


def run_ask(args: argparse.Namespace) -> None:
    ctx = load_ctx(args)
    print_json(message.ask(ctx, himalaya.Himalaya(ctx), args.n, args.question))


def run_code(args: argparse.Namespace) -> None:
    ctx = load_ctx(args)
    print_json(message.codes(ctx, himalaya.Himalaya(ctx), args.mailbox, args.minutes))


def run_attachments(args: argparse.Namespace) -> None:
    ctx = load_ctx(args)
    print_json(message.attachments(ctx, himalaya.Himalaya(ctx), args.n))


def run_attachment_send(args: argparse.Namespace) -> None:
    ctx = load_ctx(args)
    print_json(message.attachment_send(ctx, himalaya.Himalaya(ctx), args.n, args.index))


def run_mark_read(args: argparse.Namespace) -> None:
    ctx = load_ctx(args)
    print_json(message.mark_read(ctx, himalaya.Himalaya(ctx), args.n))


def run_move(args: argparse.Namespace) -> None:
    ctx = load_ctx(args)
    print_json(message.move(ctx, himalaya.Himalaya(ctx), args.n, args.to))


def read_text() -> str:
    """The text on stdin, as UTF-8 (a byte order mark is dropped)."""
    try:
        return read_stdin_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise MailError("the text on stdin is not UTF-8") from None


def run_draft(args: argparse.Namespace) -> None:
    body = read_text()
    ctx = load_ctx(args)
    print_json(compose.new_draft(
        ctx, args.mailbox, args.to, args.cc, args.bcc, args.subject, body, args.attach))


def run_reply(args: argparse.Namespace) -> None:
    ctx = load_ctx(args)
    print_json(compose.reply(
        ctx, himalaya.Himalaya(ctx), args.n, args.intent, args.all, args.mailbox))


def run_forward(args: argparse.Namespace) -> None:
    note = "" if vault.at_terminal() else read_text()  # a person at a terminal is not asked
    ctx = load_ctx(args)
    print_json(compose.forward(ctx, himalaya.Himalaya(ctx), args.n, args.to, note, args.mailbox))


def run_revise(args: argparse.Namespace) -> None:
    ctx = load_ctx(args)
    print_json(compose.revise(ctx, himalaya.Himalaya(ctx), args.draft, args.instruction))


def run_draft_update(args: argparse.Namespace) -> None:
    body = read_text() if args.body else None
    ctx = load_ctx(args)
    print_json(compose.update(
        ctx, args.draft, to=args.to, cc=args.cc, bcc=args.bcc, subject=args.subject,
        attach=args.attach, body=body))


def run_confirm(args: argparse.Namespace) -> None:
    print_json(compose.confirm(load_ctx(args), args.draft))


def run_send(args: argparse.Namespace) -> None:
    ctx = load_ctx(args)
    print_json(compose.send(ctx, himalaya.Himalaya(ctx), args.draft, args.code))


def run_drafts(args: argparse.Namespace) -> None:
    print_json(compose.listing(load_ctx(args)))


def run_discard(args: argparse.Namespace) -> None:
    print_json(compose.discard(load_ctx(args), args.draft))


def run_watch(args: argparse.Namespace) -> None:
    """The alert watcher, which the alert service runs: until it is stopped."""
    watch.serve(load_ctx(args))


def run_hook(args: argparse.Namespace) -> None:
    """What carillon runs for each new mail: it notes the mailbox for the watcher, and nothing
    more. Whatever carillon passes after the mailbox is ignored."""
    watch.poke(load_ctx(args).state, args.mailbox)


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

    p = sub.add_parser(
        "secret-delete", help="delete the stored password or sign-in token of a mailbox")
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
        "configure", help="write the himalaya, carillon and ortie configs from mailboxes.toml")
    p.set_defaults(func=run_configure)

    p = sub.add_parser(
        "oauth-login", help="sign an Outlook mailbox in (do it yourself, in a terminal)")
    p.add_argument("mailbox")
    p.add_argument(
        "--paste", action="store_true",
        help="sign in by pasting the address the browser ends on, if the device sign-in is refused")
    p.set_defaults(func=run_oauth_login)

    # No help text, like secret-fetch: only ortie calls it.
    p = sub.add_parser("token-store")
    p.add_argument("action", choices=["read", "write"])
    p.add_argument("mailbox")
    p.set_defaults(func=run_token_store)

    p = sub.add_parser(
        "folders", help="list the folders of a mailbox and guess its sent, trash and archive")
    p.add_argument("mailbox")
    p.set_defaults(func=run_folders)

    p = sub.add_parser(
        "check", help="test that each mailbox signs in and answers (one mailbox, or all)")
    p.add_argument("mailbox", nargs="?")
    p.set_defaults(func=run_check)

    p = sub.add_parser(
        "list", help="search the mailboxes: one numbered list, newest first (the numbers are "
        "what the other commands take)")
    p.add_argument(
        "--mailbox", action="append", default=[], metavar="M",
        help="search this mailbox (repeat for several; default: all)")
    p.add_argument(
        "--folder", choices=FOLDER_ROLES, default="inbox",
        help="which folder of each mailbox (default: inbox)")
    p.add_argument(
        "--since", type=day, metavar="YYYY-MM-DD", help="mail dated on or after this day")
    p.add_argument(
        "--before", type=day, metavar="YYYY-MM-DD", help="mail dated before this day")
    p.add_argument(
        "--from", dest="sender", metavar="TEXT", help="the sender's name or address contains TEXT")
    p.add_argument("--subject", metavar="TEXT", help="the subject contains TEXT")
    p.add_argument("--body", metavar="TEXT", help="the body contains TEXT")
    p.add_argument("--unseen", action="store_true", help="only mail not marked as read")
    p.add_argument(
        "--limit", type=positive_int, default=20, metavar="N",
        help="at most N mails in all (default: 20)")
    p.set_defaults(func=run_list)

    p = sub.add_parser(
        "reader-check", help="test the mail reader (claude -p with no tools) on a sample mail")
    p.set_defaults(func=run_reader_check)

    p = sub.add_parser(
        "rules", help="print the rules mail is sorted by: the app's defaults, then the user's "
        "own (mail/rules.md), which win")
    p.set_defaults(func=run_rules)

    p = sub.add_parser(
        "chat-set", help="record this chat as the private chat: drafts, codes and alerts go "
        "there (run it from the agent in that chat)")
    p.set_defaults(func=run_chat_set)

    p = sub.add_parser("chat-show", help="show the recorded private chat")
    p.set_defaults(func=run_chat_show)

    number = "the number of the mail in the last list"
    p = sub.add_parser(
        "show", help="send the original of a listed mail to the private chat (it is not printed)")
    p.add_argument("n", type=positive_int, help=number)
    p.set_defaults(func=run_show)

    p = sub.add_parser(
        "look", help="the reader's verdict on a listed mail: summary, code, amount, deadline, "
        "tier, category, reason, phishing")
    p.add_argument("n", type=positive_int, help=number)
    p.set_defaults(func=run_look)

    p = sub.add_parser(
        "ask", help="ask the reader about a listed mail; the answer goes to the private chat")
    p.add_argument("n", type=positive_int, help=number)
    p.add_argument("--question", required=True, metavar="TEXT", help="the user's question")
    p.set_defaults(func=run_ask)

    p = sub.add_parser(
        "code", help="verification codes in the inbox mail of the last few minutes")
    p.add_argument(
        "--mailbox", action="append", default=[], metavar="M",
        help="look in this mailbox (repeat for several; default: all)")
    p.add_argument(
        "--minutes", type=positive_int, default=15, metavar="N",
        help="mail of the last N minutes (default: 15)")
    p.set_defaults(func=run_code)

    p = sub.add_parser("attachments", help="the attachments of a listed mail")
    p.add_argument("n", type=positive_int, help=number)
    p.set_defaults(func=run_attachments)

    p = sub.add_parser(
        "attachment-send", help="send an attachment of a listed mail to the private chat")
    p.add_argument("n", type=positive_int, help=number)
    p.add_argument("index", type=positive_int, help="the number of the attachment (attachments)")
    p.set_defaults(func=run_attachment_send)

    p = sub.add_parser("mark-read", help="mark a listed mail, and no other, as read")
    p.add_argument("n", type=positive_int, help=number)
    p.set_defaults(func=run_mark_read)

    p = sub.add_parser(
        "move", help="move a listed mail to the archive or trash folder of its mailbox "
        "(nothing is deleted)")
    p.add_argument("n", type=positive_int, help=number)
    p.add_argument("--to", required=True, choices=message.MOVE_TARGETS, help="where to")
    p.set_defaults(func=run_move)

    address = "an email address (repeat for several)"
    p = sub.add_parser(
        "draft", help="write a mail, the body on stdin: the draft and the code that sends it go "
        "to the private chat, never here")
    p.add_argument("--mailbox", required=True, metavar="M", help="the mailbox it is sent from")
    p.add_argument("--to", action="append", required=True, metavar="A", help=address)
    p.add_argument("--cc", action="append", default=[], metavar="A", help=address)
    p.add_argument("--bcc", action="append", default=[], metavar="A", help=address)
    p.add_argument("--subject", required=True, metavar="S")
    p.add_argument(
        "--attach", action="append", default=[], metavar="PATH", help="a file (repeat for several)")
    p.set_defaults(func=run_draft)

    p = sub.add_parser(
        "reply", help="the reader writes a reply to a listed mail as the user wants it; the "
        "draft goes to the private chat")
    p.add_argument("n", type=positive_int, help=number)
    p.add_argument("--intent", required=True, metavar="TEXT", help="what the user wants to say")
    p.add_argument(
        "--all", action="store_true", help="copy everyone the mail went to (To and Cc)")
    p.add_argument(
        "--mailbox", metavar="M", help="send from this mailbox (default: the mail's own)")
    p.set_defaults(func=run_reply)

    p = sub.add_parser(
        "forward", help="forward a listed mail as an attachment; a note for it on stdin")
    p.add_argument("n", type=positive_int, help=number)
    p.add_argument("--to", action="append", required=True, metavar="A", help=address)
    p.add_argument(
        "--mailbox", metavar="M", help="send from this mailbox (default: the mail's own)")
    p.set_defaults(func=run_forward)

    draft_id = "the id of the draft, such as d1a2b3c"
    p = sub.add_parser(
        "revise", help="the reader rewrites a reply it wrote as the user says (mx reply drafts "
        "only)")
    p.add_argument("draft", help=draft_id)
    p.add_argument("--instruction", required=True, metavar="TEXT", help="the user's words")
    p.set_defaults(func=run_revise)

    given = "replaces the old list; repeat for several, \"\" for none"
    p = sub.add_parser(
        "draft-update", help="change a draft: the change and a new code go to the private chat")
    p.add_argument("draft", help=draft_id)
    p.add_argument("--to", action="append", metavar="A", help=given)
    p.add_argument("--cc", action="append", metavar="A", help=given)
    p.add_argument("--bcc", action="append", metavar="A", help=given)
    p.add_argument("--subject", metavar="S")
    p.add_argument("--attach", action="append", metavar="PATH", help=given)
    p.add_argument(
        "--body", action="store_true", help="a new body, on stdin, written by you")
    p.set_defaults(func=run_draft_update)

    p = sub.add_parser(
        "confirm", help="a new code for a draft (after the old one expired, was void, or the "
        "send failed)")
    p.add_argument("draft", help=draft_id)
    p.set_defaults(func=run_confirm)

    p = sub.add_parser(
        "send", help="send a draft with the code the user replied with in the chat")
    p.add_argument("draft", help=draft_id)
    p.add_argument("--code", required=True, metavar="CODE", help="the user's code")
    p.set_defaults(func=run_send)

    p = sub.add_parser("drafts", help="the drafts not sent yet")
    p.set_defaults(func=run_drafts)

    p = sub.add_parser("discard", help="throw a draft away")
    p.add_argument("draft", help=draft_id)
    p.set_defaults(func=run_discard)

    p = sub.add_parser(
        "watch", help="watch the inboxes and alert on urgent mail in the private chat (the alert "
        "service runs it; it runs until it is stopped)")
    p.set_defaults(func=run_watch)

    # No help text, like secret-fetch: only carillon calls it.
    p = sub.add_parser("hook")
    p.add_argument("mailbox")
    p.add_argument("ignored", nargs=argparse.REMAINDER)
    p.set_defaults(func=run_hook)

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

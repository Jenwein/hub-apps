"""Signing an Outlook mailbox in: the ortie calls behind `oauth-login`.

Every call runs in the state directory with the relative config name (`configs.ortie_argv`). The
calls whose answer mx reads are captured, with their stdin closed; the one that waits for the
person (`auth resume`) runs on the person's terminal.
"""

from __future__ import annotations

import json
import subprocess
import sys

from . import configs
from .context import Ctx
from .settings import MailError
from .tools import NO_WINDOW


def ortie_json(ctx: Ctx, ortie: str, mailbox: str, *args: str) -> dict:
    """Run ortie in the state directory and read its JSON answer. A failure is a MailError with
    the error ortie gives."""
    try:
        done = subprocess.run(
            configs.ortie_argv(ortie, mailbox, "--json", *args), cwd=ctx.state, check=False,
            stdin=subprocess.DEVNULL, capture_output=True, timeout=60, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise MailError(f"cannot run ortie: {err}") from err
    try:
        answer = json.loads(done.stdout)
    except ValueError:  # not JSON, or bytes that are not UTF-8
        answer = None
    if done.returncode != 0:
        said = answer.get("error") if isinstance(answer, dict) else None
        reason = said[:300] if isinstance(said, str) and said else f"exit {done.returncode}"
        raise MailError(f"ortie said: {reason}")
    if not isinstance(answer, dict):
        raise MailError("ortie answered something that is not JSON")
    return answer


def ortie_attached(ctx: Ctx, ortie: str, mailbox: str, *args: str) -> None:
    """Run ortie on the terminal the user is at: it waits for the user, and shows its progress."""
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        done = subprocess.run(  # no capture, no NO_WINDOW: this one talks to the person
            configs.ortie_argv(ortie, mailbox, *args), cwd=ctx.state, check=False)
    except OSError as err:
        raise MailError(f"cannot run ortie: {err}") from err
    if done.returncode != 0:
        raise MailError(f"ortie could not finish the sign-in (exit {done.returncode})")


def answer_field(answer: dict, key: str) -> str:
    value = answer.get(key)
    if not isinstance(value, str) or not value:
        raise MailError(f"ortie's answer has no {key}")
    return value


def sign_in_with_device(ctx: Ctx, ortie: str, name: str) -> None:
    """The device grant: the user opens an address on any device and types a short code there."""
    try:
        answer = ortie_json(ctx, ortie, name, "auth", "get")
        device_code = answer_field(answer, "device_code")
        user_code = answer_field(answer, "user_code")
        address = answer_field(answer, "verification_uri")
        print(f"On any device, open {address} and enter the code {user_code}", file=sys.stderr)
        print("Waiting until you have approved the sign-in...", file=sys.stderr, flush=True)
        ortie_attached(ctx, ortie, name, "auth", "resume", device_code)
    except MailError as err:
        raise MailError(
            f"{err}\nIf Microsoft refuses the device sign-in, run it again with --paste: "
            f"uv run apps/mail/scripts/mx.py oauth-login {name} --paste") from None


def sign_in_with_code(ctx: Ctx, ortie: str, name: str) -> None:
    """The authorization-code grant: the browser ends on https://localhost/?code=..., which does
    not load, and the user pastes that address here."""
    answer = ortie_json(ctx, ortie, name, "auth", "get")
    address = answer_field(answer, "authorization_uri")
    state = answer_field(answer, "state")
    verifier = answer_field(answer, "pkce_code_verifier")
    print(f"Open this address in a browser and sign in:\n{address}\n", file=sys.stderr)
    print(
        "The browser ends on a page that does not load, https://localhost/?code=...\n"
        "Copy its address from the address bar and paste it here: ",
        end="", file=sys.stderr, flush=True)
    try:
        pasted = input().strip()
    except EOFError:
        raise MailError("no address given: nothing signed in") from None
    if not pasted or pasted.startswith("-"):
        raise MailError("that is not an address: nothing signed in")
    ortie_attached(
        ctx, ortie, name, "auth", "resume", "--state", state, "--pkce", verifier, pasted)


def sign_in(ctx: Ctx, ortie: str, name: str, *, paste: bool) -> None:
    """Run the sign-in, then ask ortie whether it left a token."""
    if paste:
        sign_in_with_code(ctx, ortie, name)
    else:
        sign_in_with_device(ctx, ortie, name)
    try:
        ortie_json(ctx, ortie, name, "token", "inspect")
    except MailError as err:
        raise MailError(f"the sign-in left no token for {name}: {err}") from None

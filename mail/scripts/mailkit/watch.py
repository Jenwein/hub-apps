"""The alert watcher, `mx watch`: a long-running process that the alert service starts.

It supervises carillon, which holds an IMAP IDLE connection to every inbox and runs `mx hook
<mailbox>` for each new mail; the hook only touches `state/pokes/<mailbox>`. Every TICK_SECONDS
the watcher:

0. reads `mail/mailboxes.toml` and `mail/config.toml` again when their text changed (`_reload`):
   the configs are written again, and carillon is started again when the mailboxes changed. A new
   mailbox (or one whose address, server or inbox changed) is swept at once and, like a first
   run, only gets a baseline; a removed one is no longer swept; a new language or reader model
   is used from then on. When alerts were switched off, or are now meant for another machine,
   the watcher stops: carillon is stopped and `run` returns, which ends `mx watch` with status 0.
   Settings that cannot be read leave the old ones in place.
1. checks that carillon runs. One that exited, or could not be started, starts a down spell in
   `Health` and is started again after the next step of RESTART_BACKOFF; the steps start over once
   a carillon has kept running for STABLE_SECONDS, which also ends the spell.
2. reads the pokes. The first poke of a mailbox starts a batch that runs BATCH_SECONDS later, so a
   burst of mail is sorted together; the pokes are removed when the batch runs.
3. sweeps every mailbox when SWEEP_SECONDS have passed since the last sweep, and at its first tick:
   carillon reports nothing that arrived before it started or while its connection was down, and
   an IDLE connection can die without a word. A sweep covers the batches that wait.
4. processes the mailboxes that are due (`process`).
5. sends the messages that wait in the alert queue.
6. tells the user once when carillon has been down for longer than GRACE.
7. writes the heartbeat, `state/watch.json`.

`process` finds each mailbox's new mail with `marks.fresh`, sorts it with `triage`, and alerts the
mail of the `now` tier that was never alerted: one message per mail, or one for all of them when
there are more than COMBINE_OVER. A message the chat does not take is queued and sent later, and
the mail counts as alerted either way, so it is never alerted twice. A mailbox's new mark is saved
only after its mail was sorted and alerted: mail that a failure or the triage budget left unsorted
is found again by the next sweep, and what was sorted is not sorted again (the verdicts are kept).
A first run only sets the marks: the mail already there is not news. Login, connection and reader
failures go to `Health`, which says when the user should hear of them: once per spell.

The state lock is never held across a call that takes it itself (the marks, the verdicts, triage,
health, the queue): it is not reentrant.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from . import clock as clock_module
from . import configs, context
from .alerts import COMBINE_OVER, AlertQueue, alert_text, combined_text
from .chat import NO_CHAT, ChatError, saved, send_text
from .context import Ctx
from .health import GRACE, HEARTBEAT, Health, heartbeat, sign_in_command
from .himalaya import AuthError, Himalaya
from .marks import (
    MAX_CATCH_UP,
    Mark,
    drop_marks,
    fresh,
    inbox_folder,
    load_marks,
    save_marks,
    verdict_key,
)
from .paths import read_json, this_machine, write_json
from .query import clean
from .reader import Reader, ReaderError, Verdict
from .settings import NAME_RE, Mailbox, MailError
from .strings import t
from .tools import NO_WINDOW, kill_tree, version_problem
from .triage import triage
from .verdicts import VerdictStore

BATCH_SECONDS = 90
SWEEP_SECONDS = 900
TICK_SECONDS = 5
RESTART_BACKOFF = (5, 30, 120, 300)
STABLE_SECONDS = 60  # a carillon that ran this long was up: its down spell ends
TRIAGE_BUDGET = 600.0  # seconds one mailbox's triage may start batches for
DRAIN_RETRY_SECONDS = 60  # a chat that failed is asked again after this long, not every tick
STOP_SECONDS = 30  # how long carillon has to stop on SIGTERM before it is killed
DETAIL_LIMIT = 300  # characters of an error in a notice and in the log
LOG_LIMIT = 1_000_000  # bytes: a longer watch.log is moved to watch.log.1 when the watcher starts
# The failures that a mailbox which answered has shown to be over: it says nothing of the reader.
LOGIN_KINDS = ("auth", "connect")

POKES = "pokes"  # in the state directory: one empty file per mailbox with news
SETTINGS = ("mailboxes.toml", "config.toml")  # in the hub's mail directory: read again on a change
LOG = "watch.log"  # in the state directory: carillon's output and the watcher's own lines
LOCK = "watch.lock"  # in the state directory: held by the one watcher of this hub


class Process(Protocol):
    """carillon as the watcher sees it (`Carillon`, or a fake in the tests)."""

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int | None: ...

    def kill(self) -> None: ...


def _passed(start: datetime, seconds: float, now: datetime) -> bool:
    """Have `seconds` passed since `start`? Also when the clock went back past `start`, so that a
    clock that was set back stops nothing for long."""
    return now < start or (now - start).total_seconds() >= seconds


def _stderr(line: str) -> None:
    if sys.stderr is not None:  # none under a windowless Python
        print(f"mx watch: {line}", file=sys.stderr, flush=True)


class StopWatching(Exception):
    """Alerts were switched off, or are meant for another machine now: the watcher stops."""


def _settings_text(data: Path) -> tuple[bytes | None, ...]:
    """The bytes of the files of SETTINGS (None for one that cannot be read): what tells the
    watcher that they changed."""
    found = []
    for name in SETTINGS:
        try:
            found.append((Path(data) / name).read_bytes())
        except OSError:
            found.append(None)
    return tuple(found)


def _other_account(old: Mailbox, new: Mailbox) -> bool:
    """Is `new` another account than `old` for the marks: another address, server or inbox?"""
    return (old.address, old.imap, inbox_folder(old)) != (new.address, new.imap, inbox_folder(new))


class Watcher:
    """See the module's docstring. `send` takes a chat message and raises ChatError when the chat
    cannot be reached; `spawn` starts carillon; `clock` tells the time; `log` takes a line for
    the log; `sleep` waits between ticks."""

    def __init__(self, ctx: Ctx, *, hima: Himalaya, reader: Reader,
                 send: Callable[[str], None], spawn: Callable[[], Process],
                 clock: Callable[[], datetime], log: Callable[[str], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.ctx = ctx
        self.hima = hima
        self.reader = reader
        self.send = send
        self.spawn = spawn
        self.clock = clock
        self.log = log or _stderr
        self.sleep = sleep
        self.language = ctx.settings.language
        self.health = Health(ctx)
        self.queue = AlertQueue(ctx)
        self.store = VerdictStore(ctx)
        self.proc: Process | None = None
        self.started: datetime | None = None  # when the running carillon was started
        self.stable = False  # the running carillon has kept running for STABLE_SECONDS
        self.failures = 0  # carillons in a row that did not keep running
        self.retry: tuple[datetime, int] | None = None  # when carillon died, and the wait
        self.batches: dict[str, datetime] = {}  # mailbox -> when its first poke was seen
        self.swept: datetime | None = None  # when this run swept last
        self.waiting: bool | None = None  # are messages queued (None: not looked yet)
        self.drain_after: datetime | None = None  # the chat failed: not asked before this
        self.first = True
        self.stopping = False
        self.seen = _settings_text(ctx.data)  # the settings as `ctx` read them
        self.new: set[str] = set()  # mailboxes a reload added: swept in the same tick

    # --- one tick ------------------------------------------------------------------------------

    def tick(self) -> None:
        """One pass: see the module's docstring. Raises StopWatching when the watcher must stop."""
        now = self.clock()
        self._reload(now)
        if self.first:
            self.first = False
            self._recover(now)
        self._carillon(now)
        due = self._due(now)
        sweep = self.swept is None or _passed(self.swept, SWEEP_SECONDS, now)
        if sweep:
            due = list(self.ctx.mailboxes)
            self.swept = now
        else:
            due += [name for name in self.ctx.mailboxes if name in self.new and name not in due]
        self.new.clear()
        self._take_pokes(due)
        if due:
            self.process(due)
        if sweep:
            self.health.sweep_done(now)
        now = self.clock()
        self._drain(now)
        if self.health.carillon_notice_due(now):
            self._deliver(t(
                "carillon_down", self.language, minutes=int(GRACE.total_seconds() // 60),
                every=SWEEP_SECONDS // 60, log=str(self.ctx.state / LOG)), [], now)
        self._beat(now)

    def _reload(self, now: datetime) -> None:
        """Take in settings that changed: see step 0 of the module's docstring."""
        seen = _settings_text(self.ctx.data)
        if seen == self.seen:
            return
        self.seen = seen
        try:
            ctx = context.load(self.ctx.hub)
        except MailError as err:
            self.log(f"the mail settings changed but cannot be read; the old ones stay: {err}")
            return
        try:
            check_here(ctx)
        except MailError as err:
            raise StopWatching(str(err)) from None
        old, self.ctx = self.ctx, ctx
        self.language = ctx.settings.language
        self.reader.model = ctx.settings.reader_model
        self.hima.ctx = ctx
        self.health, self.queue, self.store = Health(ctx), AlertQueue(ctx), VerdictStore(ctx)
        try:
            configs.write_configs(ctx)
        except MailError as err:
            self.log(f"cannot write the configs: {err}")
        added = [name for name, mb in ctx.mailboxes.items()
                 if name not in old.mailboxes or _other_account(old.mailboxes[name], mb)]
        removed = [name for name in old.mailboxes if name not in ctx.mailboxes]
        if added:
            drop_marks(ctx, added)  # a mark of another account, or of an earlier setup
            self.new.update(added)
        for name in removed:
            self.batches.pop(name, None)
            with suppress(OSError):
                (ctx.state / POKES / name).unlink()
            self.health.mailbox_ok(name, now)
        self.log(f"the mail settings changed: added {added or 'none'}, removed {removed or 'none'}")
        if ctx.mailboxes != old.mailboxes:
            self.log("carillon is started again with the new mailboxes")
            self.stop()
            self.retry = None  # started again in this tick, also one that waited to

    def _recover(self, now: datetime) -> None:
        """A heartbeat left behind: the watcher before stopped without a word (it was killed, or
        the machine went down), and nothing watched since its last tick."""
        beat = heartbeat(self.ctx.state)
        if beat is not None:
            self.log(f"the last watcher stopped without a word after {beat.isoformat()}")
            self.health.carillon_down(min(beat, now))

    def _beat(self, now: datetime) -> None:
        data = {
            "pid": os.getpid(), "at": now.isoformat(),
            "carillon": "up" if self.proc is not None else "down",
            "last_sweep": self.swept.isoformat() if self.swept else None}
        try:
            write_json(self.ctx.state / HEARTBEAT, data)
        except OSError as err:
            self.log(f"cannot write the heartbeat: {err}")

    # --- carillon ------------------------------------------------------------------------------

    def _carillon(self, now: datetime) -> None:
        if self.proc is not None:
            code = self.proc.poll()
            if code is None:
                if not self.stable and _passed(self.started, STABLE_SECONDS, now):
                    self.stable, self.failures = True, 0
                    self.health.carillon_up(self.started)
                return
            self.log(f"carillon exited with status {code}")
            self.proc = None
            self._down(now)
        if self.retry is None or _passed(*self.retry, now):
            self._start(now)

    def _down(self, now: datetime) -> None:
        self.health.carillon_down(now)
        wait = RESTART_BACKOFF[min(self.failures, len(RESTART_BACKOFF) - 1)]
        self.failures += 1
        self.retry = (now, wait)
        self.log(f"carillon is started again in {wait} s")

    def _start(self, now: datetime) -> None:
        if self.stopping:
            return
        try:
            proc = self.spawn()
        except (OSError, MailError) as err:
            self.log(f"carillon could not be started: {err}")
            self._down(now)
            return
        self.proc, self.started, self.stable, self.retry = proc, now, False, None

    def _terminate(self) -> None:
        """Ask carillon to stop, without waiting for it."""
        if self.proc is not None:
            with suppress(OSError):
                self.proc.terminate()

    def stop(self) -> None:
        """Stop carillon: it has STOP_SECONDS to end, then it is killed."""
        proc, self.proc = self.proc, None
        if proc is None:
            return
        with suppress(OSError):
            proc.terminate()
        try:
            proc.wait(timeout=STOP_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()

    # --- pokes ---------------------------------------------------------------------------------

    def _due(self, now: datetime) -> list[str]:
        """The mailboxes whose batch is due. A poke starts a batch; a poke of a mailbox this hub
        does not have is removed."""
        folder = self.ctx.state / POKES
        try:
            names = [path.name for path in folder.iterdir()]
        except FileNotFoundError:
            names = []
        except OSError as err:
            self.log(f"cannot read the pokes: {err}")
            names = []
        for name in names:
            if name in self.ctx.mailboxes:
                self.batches.setdefault(name, now)
            else:
                with suppress(OSError):
                    (folder / name).unlink()
        return [
            name for name in self.ctx.mailboxes
            if name in self.batches and _passed(self.batches[name], BATCH_SECONDS, now)]

    def _take_pokes(self, names: list[str]) -> None:
        """Remove the pokes and batches of `names` before their mail is looked at: a mail that
        comes meanwhile pokes again and starts a new batch."""
        for name in names:
            self.batches.pop(name, None)
            with suppress(OSError):
                (self.ctx.state / POKES / name).unlink()

    # --- the queue -----------------------------------------------------------------------------

    def _queued(self) -> bool:
        if self.waiting is None:
            self.waiting = bool(self.queue.pending())
        return self.waiting

    def _drain(self, now: datetime) -> None:
        if not self._queued() or (self.drain_after is not None and now < self.drain_after):
            return
        sent = self.queue.drain(self.send, now)
        self.waiting = bool(self.queue.pending())
        if self.waiting:
            self.drain_after = now + timedelta(seconds=DRAIN_RETRY_SECONDS)
            self.log(f"the chat cannot be reached: {sent} queued messages sent, the rest wait")
        else:
            self.drain_after = None

    def _deliver(self, text: str, keys: list[str], at: datetime) -> None:
        """Send `text` to the chat, or queue it when the chat cannot take it now. Messages that
        wait go first: a new one is queued behind them."""
        if self._queued():
            self._drain(at)
        if not self._queued():
            try:
                self.send(text)
                return
            except ChatError as err:
                self.log(f"the chat cannot be reached, the message waits: {err}")
                self.drain_after = at + timedelta(seconds=DRAIN_RETRY_SECONDS)
        self.queue.add(text, keys, at)
        self.waiting = True

    # --- the mail ------------------------------------------------------------------------------

    def process(self, names: list[str]) -> None:
        """Find, sort and alert the new mail of the mailboxes `names` (see the module's
        docstring)."""
        marks = load_marks(self.ctx)
        urgent: dict[str, Verdict] = {}
        moved: dict[str, Mark] = {}
        for name in names:
            mb = self.ctx.mailboxes.get(name)
            if mb is None:
                continue
            found = self._mailbox(mb, marks.get(name))
            if found is not None:
                verdicts, mark = found
                for v in verdicts:
                    if v.tier == "now" and v.key not in urgent and not self.store.alerted(v.key):
                        urgent[v.key] = v
                if mark is not None:
                    moved[name] = mark
            self._beat(self.clock())  # a sweep of slow mailboxes is no stopped watcher
        self._alert(list(urgent.values()), self.clock())
        if moved:
            save_marks(self.ctx, moved)

    def _mailbox(
        self, mb: Mailbox, mark: Mark | None
    ) -> tuple[list[Verdict], Mark | None] | None:
        """The verdicts of the new mail of `mb` and the mark to save once they are alerted (None:
        keep the old one); None when the mailbox failed."""
        at = self.clock()
        try:
            uids, new, skipped = fresh(self.hima, mb, mark, at)
        except MailError as err:
            self._failed(mb, "auth" if isinstance(err, AuthError) else "connect", err, at)
            return None
        if skipped:
            self.log(f"{mb.name}: {skipped} new mails were not sorted, more than "
                     f"{MAX_CATCH_UP} came")
        if not uids:
            self.health.mailbox_ok(mb.name, at, kinds=LOGIN_KINDS)
            return [], (new if new != mark else None)
        folder = inbox_folder(mb)
        items = [(verdict_key(mb.name, new.uidvalidity, str(uid)), mb.name, folder, uid)
                 for uid in uids]
        try:
            result = triage(self.ctx, self.hima, self.reader, items, budget=TRIAGE_BUDGET)
        except ReaderError as err:  # the reader did not answer: the old mark stays
            self._failed(mb, "reader", err, at)
            return None
        except MailError as err:  # the bodies could not be fetched: the spell of fresh goes on
            self._failed(mb, "auth" if isinstance(err, AuthError) else "connect", err, at)
            return None
        # Only now has the mailbox answered everything it was asked: a failure while fetching the
        # mail must not end the spell that the same failure goes on with at the next sweep.
        self.health.mailbox_ok(mb.name, at, kinds=LOGIN_KINDS)
        if result.verdicts:
            # None of the mail got a verdict of the reader, and it failed on some of it now.
            if result.fallbacks and all(v.fallback for v in result.verdicts):
                self._unsorted(mb, len(result.verdicts), at)
            else:
                self.health.mailbox_ok(mb.name, at)
        if result.left:
            self.log(f"{mb.name}: {len(result.left)} new mails were not sorted in time; the "
                     "next sweep sorts them")
            return result.verdicts, None
        return result.verdicts, new

    def _failed(self, mb: Mailbox, kind: str, err: MailError, at: datetime) -> None:
        detail = clean(str(err), DETAIL_LIMIT)
        self.log(f"{mb.name}: {kind} failure: {detail}")
        if not self.health.mailbox_failed(mb.name, kind, detail, at):
            return
        if kind == "auth":
            text = t("login_failed", self.language, mailbox=mb.name, fix=sign_in_command(mb))
        elif kind == "connect":
            text = t("cannot_connect", self.language, mailbox=mb.name,
                     minutes=int(GRACE.total_seconds() // 60), why=detail)
        else:
            text = t("reader_down", self.language, mailbox=mb.name, why=detail)
        self._deliver(text, [], at)

    def _unsorted(self, mb: Mailbox, count: int, at: datetime) -> None:
        """None of the new mail could be sorted: each got a fallback. That says more about the
        reader than about the mail, so it is a reader failure."""
        detail = f"none of {count} new mails could be sorted"
        self.log(f"{mb.name}: reader failure: {detail}")
        if self.health.mailbox_failed(mb.name, "reader", detail, at):
            self._deliver(t("reader_unsorted", self.language, mailbox=mb.name, count=count),
                          [], at)

    def _alert(self, urgent: list[Verdict], at: datetime) -> None:
        if len(urgent) > COMBINE_OVER:
            messages = [(combined_text(urgent, self.language), [v.key for v in urgent])]
        else:
            messages = [(alert_text(v, self.language), [v.key]) for v in urgent]
        for text, keys in messages:
            self._deliver(text, keys, at)
            for key in keys:
                self.store.mark_alerted(key, at)

    # --- running -------------------------------------------------------------------------------

    def _on_signal(self, signum, frame) -> None:
        """SIGTERM (the service stops) or Ctrl+C: carillon is told to stop at once, then the
        watcher stops, after the reader calls that run finished."""
        if self.stopping:
            return
        self.stopping = True
        self._terminate()
        raise KeyboardInterrupt

    def _signals(self) -> dict:
        if threading.current_thread() is not threading.main_thread():
            return {}
        previous = {}
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            number = getattr(signal, name, None)
            if number is not None:
                previous[number] = signal.signal(number, self._on_signal)
        return previous

    def run(self) -> None:
        """`tick` every TICK_SECONDS until SIGTERM or Ctrl+C, or until alerts are switched off or
        moved to another machine; then carillon is stopped. A watcher that was told to stop
        removes its heartbeat (one that dies of an error leaves it). A tick that fails for want of
        the state (a lock held too long, a file that cannot be written) is logged, and the next
        tick tries again."""
        previous = self._signals()
        told = False
        try:
            while True:
                try:
                    self.tick()
                except (MailError, OSError) as err:
                    self.log(f"a tick failed: {err}")
                self.sleep(TICK_SECONDS)
        except KeyboardInterrupt:
            told = True
        except StopWatching as why:
            self.log(f"stopped: {why}")
            told = True
        finally:
            self.stopping = True
            self.stop()
            for number, handler in previous.items():
                signal.signal(number, handler)
            if told:
                with suppress(OSError):
                    (self.ctx.state / HEARTBEAT).unlink()


# --- carillon ----------------------------------------------------------------------------------


def append_fd(path: Path) -> int:
    """A file descriptor that only appends to `path` (made private if it is new): every write
    lands at the end, also when carillon, which gets one, and the watcher write the file at once.
    Windows has no O_APPEND of its own (the C runtime emulates it), so the file is opened for
    appending only, which Windows honours for every write through the handle."""
    if os.name != "nt":
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    import _winapi
    import msvcrt

    append_data, synchronize = 0x0004, 0x00100000  # FILE_APPEND_DATA, SYNCHRONIZE
    share = 0x1 | 0x2 | 0x4  # FILE_SHARE_READ, WRITE, DELETE: rotating and reading go on
    open_always, normal = 4, 0x80  # OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL
    handle = _winapi.CreateFile(
        str(path), append_data | synchronize, share, 0, open_always, normal, 0)
    return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)


def file_log(path: Path) -> Callable[[str], None]:
    """A `log` for the Watcher that appends a line with the time to `path`."""

    def log(line: str) -> None:
        stamp = clock_module.now().isoformat(timespec="seconds")
        try:
            fd = append_fd(path)
            try:
                os.write(fd, f"{stamp} mx watch: {line}\n".encode("utf-8", "replace"))
            finally:
                os.close(fd)
        except OSError:
            _stderr(line)

    return log


def rotate_log(state: Path) -> None:
    """Move a watch.log over LOG_LIMIT bytes to watch.log.1, which an older one is replaced by."""
    path = Path(state) / LOG
    with suppress(OSError):
        if path.stat().st_size > LOG_LIMIT:
            os.replace(path, path.with_name(f"{LOG}.1"))


class Carillon:
    """A carillon that `spawn_carillon` started."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc

    def poll(self) -> int | None:
        return self.proc.poll()

    def terminate(self) -> None:
        """SIGTERM: carillon stops its watchers and exits 0. Windows has no such signal: carillon
        and what it runs are killed."""
        if self.proc.poll() is not None:
            return
        if os.name == "posix":
            with suppress(OSError):
                self.proc.terminate()
        else:
            kill_tree(self.proc)

    def wait(self, timeout: float | None = None) -> int | None:
        return self.proc.wait(timeout)

    def kill(self) -> None:
        if os.name == "posix":
            with suppress(OSError):
                self.proc.kill()
            with suppress(OSError, subprocess.SubprocessError):
                self.proc.wait(timeout=10)
        else:
            kill_tree(self.proc)


def spawn_carillon(ctx: Ctx) -> Carillon:
    """Start `carillon -c carillon.toml --log-level warn watch` in the state directory, with stdin
    closed and its output appended to watch.log. It is looked up on PATH each time, so one
    installed while the watcher runs is found at the next start."""
    found = shutil.which("carillon")
    if found is None:
        raise MailError(
            "carillon is not installed: uv run apps/mail/scripts/mx.py install-tool carillon")
    argv = [os.path.abspath(found), "-c", configs.CARILLON_CONFIG, "--log-level", "warn", "watch"]
    try:
        out = append_fd(ctx.state / LOG)
    except OSError as err:
        raise MailError(f"cannot open {ctx.state / LOG}: {err}") from err
    try:
        # carillon stays in the watcher's process group: a service manager that stops the
        # watcher by killing its group stops carillon too, and none is left watching alone.
        # NO_WINDOW: under a windowless service carillon gets a hidden console, which its hooks
        # and password commands share, so no window flashes up for each of them.
        proc = subprocess.Popen(
            argv, cwd=ctx.state, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
            creationflags=NO_WINDOW)
    finally:
        os.close(out)
    return Carillon(proc)


# --- the commands ------------------------------------------------------------------------------


def poke(state: Path, mailbox: str) -> None:
    """What `mx hook <mailbox>` does for each new mail carillon sees: touch state/pokes/<mailbox>.
    Nothing else: the hook runs on carillon's thread of that mailbox and must be quick."""
    if not NAME_RE.fullmatch(mailbox):  # it names a file
        raise MailError(f"{mailbox!r} is not a mailbox name")
    folder = Path(state) / POKES
    try:
        folder.mkdir(mode=0o700, exist_ok=True)
        os.close(os.open(folder / mailbox, os.O_WRONLY | os.O_CREAT, 0o600))
    except OSError as err:
        raise MailError(f"cannot note the new mail of {mailbox}: {err}") from err


def check_here(ctx: Ctx) -> None:
    """Alerts run only when they are on, and only on the machine they were turned on for
    (`paths.this_machine`)."""
    if not ctx.settings.alerts_enabled:
        raise MailError("alerts are off: mx alerts on")
    here = this_machine(ctx.state)
    if ctx.settings.alerts_host != here:
        raise MailError(f"alerts run on {ctx.settings.alerts_host}, not on this machine ({here})")


@contextmanager
def single_instance(state: Path) -> Iterator[None]:
    """Hold `state/watch.lock` while the watcher runs: two watchers of one hub would alert the
    same mail twice. The operating system frees the lock when the process ends, however it ends."""
    try:
        fd = os.open(Path(state) / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as err:
        raise MailError(f"cannot open {Path(state) / LOCK}: {err}") from err
    try:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise MailError("mx watch is already running for this hub") from None
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                with suppress(OSError):
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


TOOLS_TOLD = "tools.json"  # in the state directory: the tool problems the chat was told of


def tools_ready(ctx: Ctx, send: Callable[[str], None], log: Callable[[str], None]) -> bool:
    """Are himalaya and carillon the pinned versions? When not, say so on stderr and in the log,
    and tell the chat once per problem (the problems told are kept in TOOLS_TOLD; a notice the
    chat does not take is tried at the next start), and return False: the watcher does not
    start, since tools of other versions may answer what mx cannot read."""
    problems = [p for p in (version_problem("himalaya"), version_problem("carillon")) if p]
    path = ctx.state / TOOLS_TOLD
    try:
        data = read_json(path, {})
    except MailError:  # unreadable: tell again
        data = {}
    told = data.get("told") if isinstance(data, dict) else None
    told = [key for key in told if isinstance(key, str)] if isinstance(told, list) else []
    keys = [problem.key for problem in problems]
    if not problems:
        if told:
            with suppress(OSError):
                write_json(path, {"told": []})
        return True
    for problem in problems:
        log(problem.message())
        if sys.stderr is not None:  # none under a windowless Python
            print(f"mx: {problem.message()}", file=sys.stderr)
    if set(keys) <= set(told):
        return False
    language = ctx.settings.language
    lines = []
    for problem in problems:
        if problem.found is None:
            lines.append(t("tool_missing", language, tool=problem.tool, pinned=problem.pinned))
        elif not problem.found:
            lines.append(t("tool_silent", language, tool=problem.tool, pinned=problem.pinned))
        else:
            lines.append(t("tool_other", language, tool=problem.tool, found=problem.found,
                           pinned=problem.pinned))
    text = t("tools_stopped", language, problems=t("separator", language).join(lines),
             commands="\n".join(
                 f"uv run apps/mail/scripts/mx.py install-tool {problem.tool}"
                 for problem in problems))
    try:
        send(text)
    except ChatError as err:
        log(f"the chat cannot be reached, the tool notice waits for the next start: {err}")
        return False
    try:
        write_json(path, {"told": keys})
    except OSError as err:
        log(f"cannot write {path}: {err}")
    return False


def chat_sender(ctx: Ctx) -> Callable[[str], None]:
    """The watcher's `send`: the recorded private chat, looked up for each message, so a chat that
    is recorded later is used. No chat recorded is a chat that cannot be reached: the message
    waits."""

    def send(text: str) -> None:
        try:
            chat = saved(ctx)
        except MailError as err:
            raise ChatError(str(err)) from err
        if chat is None:
            raise ChatError(NO_CHAT)
        send_text(chat, text)

    return send


def serve(ctx: Ctx) -> None:
    """`mx watch`: unless alerts are on and meant for this machine, and himalaya and carillon
    are the pinned versions (`tools_ready`), say so on stderr and return; that is no failure, and
    a service manager that restarts only failures leaves it stopped. Otherwise write the configs
    and watch until SIGTERM or Ctrl+C, or until alerts are switched off or moved."""
    try:
        check_here(ctx)
    except MailError as err:
        if sys.stderr is not None:  # none under a windowless Python
            print(f"mx: {err}", file=sys.stderr)
        return
    with single_instance(ctx.state):
        rotate_log(ctx.state)
        log = file_log(ctx.state / LOG)
        send = chat_sender(ctx)
        if not tools_ready(ctx, send, log):
            return
        configs.write_configs(ctx)
        Watcher(
            ctx, hima=Himalaya(ctx), reader=Reader(ctx.settings.reader_model), send=send,
            spawn=lambda: spawn_carillon(ctx), clock=clock_module.now, log=log).run()

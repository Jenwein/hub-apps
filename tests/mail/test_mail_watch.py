"""The alert watcher (mailkit.health, mailkit.alerts, mailkit.watch, `mx watch`, `mx hook`).

The watcher runs on a fake clock (`mailkit.clock.now` follows it too, for the stores), with a fake
`spawn` whose FakeProcess objects stand in for carillon and a fake `send` that keeps what it was
given or fails like an unreachable chat. Mail comes from the fake himalaya and the reader is the
fake claude (fakes.py). A test that is not about mail replaces `Watcher.process` with a recorder.
Nothing sleeps.
"""

import json
import os
import signal
import socket
import sys
import time
import tomllib
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
from fakes import FAKE_UV, FakeCarillon, carillon_body
from mailkit import clock as clock_module
from mailkit import strings, watch
from mailkit.alerts import COMBINE_OVER, AlertQueue, alert_text, combined_text
from mailkit.chat import ChatError
from mailkit.cli import main
from mailkit.configs import write_configs
from mailkit.context import load
from mailkit.health import GRACE, STALE, Health, sign_in_command
from mailkit.himalaya import AuthError, ConnectError, Himalaya
from mailkit.marks import Mark, load_marks, save_marks, verdict_key
from mailkit.paths import this_machine
from mailkit.reader import Reader, Verdict
from mailkit.settings import MailError
from mailkit.triage import TriageResult
from mailkit.verdicts import VerdictStore
from mailkit.watch import (
    BATCH_SECONDS,
    RESTART_BACKOFF,
    STABLE_SECONDS,
    SWEEP_SECONDS,
    TICK_SECONDS,
    Watcher,
)
from mailtest import record_chat, write_mailboxes

BOXES = """\
[[mailbox]]
name = "qq"
address = "me@qq.com"
provider = "qq"

[[mailbox]]
name = "gmail"
address = "me@gmail.com"
provider = "gmail"
"""

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
URGENT = {
    "summary": "A card payment failed.", "code": "", "amount": "", "deadline": "",
    "tier": "now", "category": "money", "reason": "A card payment failed.", "phishing": False}
DAILY = {**URGENT, "tier": "daily", "category": "bills", "reason": "A bill to pay."}
BOGUS = {**URGENT, "tier": "bogus"}  # an answer that does not fit: the mail falls back
MINUTE = timedelta(minutes=1)


# --- the fakes of this file --------------------------------------------------------------------


class Clock:
    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> datetime:
        self.at += timedelta(seconds=seconds)
        return self.at


class FakeProcess:
    """carillon as the watcher sees it: running until `exit` or `terminate`."""

    def __init__(self):
        self.code: int | None = None
        self.terminated = 0
        self.killed = 0

    def poll(self) -> int | None:
        return self.code

    def exit(self, code: int = 1) -> None:
        self.code = code

    def terminate(self) -> None:
        self.terminated += 1
        if self.code is None:
            self.code = 0

    def wait(self, timeout: float | None = None) -> int | None:
        return self.code

    def kill(self) -> None:
        self.killed += 1
        self.code = -9


class Spawner:
    """The watcher's `spawn`: a new FakeProcess per call, or `error` raised."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.times: list[datetime] = []
        self.made: list[FakeProcess] = []
        self.error: Exception | None = None

    def __call__(self) -> FakeProcess:
        self.times.append(self.clock())
        if self.error is not None:
            raise self.error
        self.made.append(FakeProcess())
        return self.made[-1]

    @property
    def last(self) -> FakeProcess:
        return self.made[-1]


class Chat:
    """The watcher's `send`: keeps the texts, or fails like a chat that cannot be reached."""

    def __init__(self):
        self.sent: list[str] = []
        self.down = False
        self.tries = 0

    def __call__(self, text: str) -> None:
        self.tries += 1
        if self.down:
            raise ChatError("cc-connect exited with status 1: boom")
        self.sent.append(text)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    fake = Clock(T0)
    monkeypatch.setattr(clock_module, "now", fake)  # the stores prune by mailkit.clock.now
    return fake


@pytest.fixture
def ctx(hub, clock):
    """The hub with the mailboxes qq and gmail, at T0."""
    write_mailboxes(hub, BOXES)
    return load(hub.root)


def rig(ctx, clock: Clock, **extra) -> tuple[Watcher, Chat, Spawner]:
    chat, spawn = Chat(), Spawner(clock)
    log = extra.pop("log", lambda line: None)
    watcher = Watcher(
        ctx, hima=Himalaya(ctx), reader=Reader("sonnet"), send=chat, spawn=spawn, clock=clock,
        log=log, **extra)
    return watcher, chat, spawn


def record_process(watcher: Watcher) -> list[tuple[datetime, list[str]]]:
    """Replace `process` with a recorder of (time, mailboxes)."""
    calls: list[tuple[datetime, list[str]]] = []
    watcher.process = lambda names: calls.append((watcher.clock(), list(names)))
    return calls


def poke(ctx, name: str) -> None:
    """What `mx hook <name>` does (carillon saw a new mail)."""
    watch.poke(ctx.state, name)


def eml(subject: str, when: datetime = T0, sender: str = "Bank <alerts@bank.example>") -> str:
    return (
        f"From: {sender}\nTo: me@qq.com\nSubject: {subject}\nDate: {format_datetime(when)}\n"
        f"Content-Type: text/plain; charset=utf-8\n\nThe text of {subject}.\n")


def message(uid: int, subject: str, when: datetime = T0) -> dict:
    return {
        "uid": uid, "date": when.isoformat(), "internal_date": when.isoformat(), "seen": False,
        "raw": eml(subject, when)}


def server(qq=(), gmail=(), **extra) -> dict:
    """The fake himalaya's server: the inboxes of qq (UIDVALIDITY 7) and gmail (3)."""
    return {
        "qq": {"INBOX": {"uidvalidity": "7", "messages": list(qq)}},
        "gmail": {"INBOX": {"uidvalidity": "3", "messages": list(gmail)}},
        **extra}


def verdict(**fields) -> Verdict:
    return Verdict(**{
        "key": "qq/7/1", "mailbox": "qq", "sender": "Bank", "subject": "Card declined",
        "date": T0.isoformat(), "summary": "A card payment failed.", "code": None,
        "amount": "¥128.00", "deadline": "2026-10-25", "tier": "now", "category": "money",
        "reason": "A card payment failed.", "phishing": True, **fields})


def alert_of(subject: str, mailbox: str = "qq") -> str:
    """The alert text of a mail of `eml` that the fake claude sorted with URGENT."""
    return alert_text(verdict(
        mailbox=mailbox, subject=subject, amount=None, deadline=None, phishing=False), "en")


# --- the texts ---------------------------------------------------------------------------------


def test_alert_text_en_and_zh():
    v = verdict()
    assert alert_text(v, "en") == (
        "Urgent mail in qq\nFrom: Bank\nSubject: Card declined\nWhy: A card payment failed.\n"
        "Amount: ¥128.00\nDeadline: 2026-10-25\n"
        "Warning: this mail may be phishing. Do not open its links or attachments, and do not "
        "reply to it.")
    assert alert_text(v, "zh-CN") == (
        "qq 有一封紧急邮件\n发件人：Bank\n主题：Card declined\n原因：A card payment failed.\n"
        "金额：¥128.00\n截止日期：2026-10-25\n"
        "警告：这封邮件可能是钓鱼邮件。不要打开其中的链接或附件，也不要回复。")

    plain = verdict(sender="", subject="", amount=None, deadline=None, phishing=False)
    assert alert_text(plain, "") == (
        "Urgent mail in qq\nFrom: unknown\nSubject: unknown\nWhy: A card payment failed.")
    # A field of a stored verdict stays on its line, whatever the store holds.
    assert "Subject: one two\n" in alert_text(verdict(subject="one\ntwo"), "en")


def test_combined_text_has_one_line_per_mail():
    v, plain = verdict(), verdict(key="gmail/3/9", mailbox="gmail", amount=None, deadline=None,
                                  phishing=False, sender="Shop", subject="Order\nstopped")
    assert combined_text([v, plain], "en") == (
        "2 urgent mails:\n"
        "- [qq] Bank: Card declined · A card payment failed. · ¥128.00 · 2026-10-25 · "
        "⚠ may be phishing\n"
        "- [gmail] Shop: Order stopped · A card payment failed.")
    assert combined_text([v, plain], "zh").splitlines()[0] == "2 封紧急邮件："
    assert combined_text([v], "zh").splitlines()[1].endswith("⚠ 可能是钓鱼邮件")


def test_notices_are_in_both_languages():
    keys = ("login_failed", "cannot_connect", "reader_down", "reader_unsorted", "carillon_down",
            "alert_head", "alert_reason", "alert_amount", "alert_deadline", "alert_phishing",
            "alert_phishing_short", "alert_many", "alert_backlog")
    for key in keys:
        assert key in strings.STRINGS["en"] and key in strings.STRINGS["zh"]
    assert set(strings.STRINGS["en"]) == set(strings.STRINGS["zh"])


def test_sign_in_command_names_the_mailbox_and_its_way_in(hub):
    write_mailboxes(hub, BOXES + '\n[[mailbox]]\nname = "out"\naddress = "me@outlook.com"\n'
                    'provider = "outlook"\n')
    ctx = load(hub.root)
    assert sign_in_command(ctx.mailbox("qq")) == "uv run apps/mail/scripts/mx.py secret-set qq"
    assert sign_in_command(ctx.mailbox("out")) == "uv run apps/mail/scripts/mx.py oauth-login out"


# --- health ------------------------------------------------------------------------------------


def test_health_tells_each_problem_once_per_spell(ctx):
    health = Health(ctx)
    assert health.mailbox_failed("qq", "auth", "refused", T0) is True
    assert health.mailbox_failed("qq", "auth", "refused", T0 + 15 * MINUTE) is False
    # The daily report asks the same record: the spell was told already, whoever told it.
    assert Health(ctx).mailbox_failed("qq", "auth", "refused", T0 + 20 * MINUTE) is False
    # The kind changes, the spell goes on; a connect failure is news once it lasted GRACE.
    assert health.mailbox_failed("qq", "connect", "timed out", T0 + 30 * MINUTE) is False
    assert health.mailbox_failed("qq", "connect", "timed out", T0 + 59 * MINUTE) is False
    assert health.mailbox_failed("qq", "connect", "timed out", T0 + 60 * MINUTE) is True
    assert health.mailbox_failed("qq", "connect", "timed out", T0 + 75 * MINUTE) is False
    health.mailbox_ok("qq", T0 + 80 * MINUTE, kinds=("reader",))  # not its kind: it goes on
    assert health.mailbox_failed("qq", "auth", "refused", T0 + 85 * MINUTE) is False
    health.mailbox_ok("qq", T0 + 90 * MINUTE)
    assert health.mailbox_failed("qq", "auth", "refused", T0 + 95 * MINUTE) is True  # a new spell
    assert health.mailbox_failed("gmail", "reader", "no answer", T0 + 95 * MINUTE) is True

    def iso(minutes: int) -> str:
        return (T0 + minutes * MINUTE).isoformat()

    assert health.problems(T0, T0 + 4 * GRACE) == [
        {"mailbox": "qq", "kind": "auth", "since": iso(0), "until": iso(30)},
        {"mailbox": "qq", "kind": "connect", "since": iso(30), "until": iso(85)},
        {"mailbox": "qq", "kind": "auth", "since": iso(85), "until": iso(90)},
        {"mailbox": "qq", "kind": "auth", "since": iso(95), "until": None},
        {"mailbox": "gmail", "kind": "reader", "since": iso(95), "until": None},
    ]
    assert health.problems(T0 + 91 * MINUTE, T0 + 94 * MINUTE) == []


def test_carillon_notice_due_once_per_down_spell(ctx):
    health = Health(ctx)
    assert health.carillon_notice_due(T0) is False  # it is not down
    health.carillon_down(T0)
    health.carillon_down(T0 + 5 * MINUTE)  # the same spell
    assert health.carillon_notice_due(T0 + GRACE) is False
    assert health.carillon_notice_due(T0 + GRACE + MINUTE) is True
    assert health.carillon_notice_due(T0 + 2 * GRACE) is False
    health.carillon_up(T0 + 70 * MINUTE)
    health.carillon_down(T0 + 80 * MINUTE)
    assert health.carillon_notice_due(T0 + 81 * MINUTE) is False
    assert health.carillon_notice_due(T0 + 80 * MINUTE + GRACE + MINUTE) is True


def test_downtime_counts_down_spells_and_a_watcher_that_stopped_without_a_word(ctx, clock):
    health = Health(ctx)
    health.carillon_down(T0)
    health.carillon_up(T0 + 10 * MINUTE)
    health.carillon_down(T0 + 20 * MINUTE)
    health.carillon_up(T0 + 40 * MINUTE)
    clock.at = T0 + 60 * MINUTE
    assert health.downtime(T0 - 60 * MINUTE, T0 + 60 * MINUTE) == 30 * MINUTE
    assert health.downtime(T0 + 5 * MINUTE, T0 + 25 * MINUTE) == 10 * MINUTE

    # A heartbeat older than STALE: no watcher has run since, so nothing watched; the time
    # counts once where it overlaps a spell.
    beat = {"pid": 1, "at": (T0 + 30 * MINUTE).isoformat(), "carillon": "up", "last_sweep": None}
    (ctx.state / "watch.json").write_text(json.dumps(beat), "utf-8")
    assert STALE < 30 * MINUTE
    assert health.downtime(T0, T0 + 60 * MINUTE) == 50 * MINUTE
    clock.at = T0 + 30 * MINUTE + STALE - MINUTE  # a long sweep, not a stopped watcher
    assert health.downtime(T0, clock.at) == 30 * MINUTE


def test_sweeps_are_recorded(ctx):
    assert Health(ctx).last_sweep() is None
    Health(ctx).sweep_done(T0)
    assert Health(ctx).last_sweep() == T0


# --- the alert queue ---------------------------------------------------------------------------


def test_queue_drains_in_order_and_stops_at_the_first_failure(ctx):
    queue = AlertQueue(ctx)
    queue.add("one", ["qq/7/1"], T0)
    queue.add("two", [], T0 + MINUTE)  # a notice: no mail in it
    queue.add("three", ["qq/7/3", "qq/7/4"], T0 + 2 * MINUTE)
    tries: list[str] = []

    def send(text: str) -> None:
        tries.append(text)
        if text == "two" and tries.count("two") == 1:
            raise ChatError("down")

    assert queue.drain(send, T0 + 5 * MINUTE) == 1
    assert [entry["text"] for entry in queue.pending()] == ["two", "three"]
    assert queue.drain(send, T0 + 10 * MINUTE) == 2
    assert queue.pending() == [] and queue.drain(send, T0 + 11 * MINUTE) == 0
    assert tries == ["one", "two", "two", "three"]
    # The alerts that went out later than they were queued, by the time they went out.
    assert queue.late(T0, T0 + 60 * MINUTE) == [
        {"keys": ["qq/7/1"], "queued": T0.isoformat(),
         "sent": (T0 + 5 * MINUTE).isoformat()},
        {"keys": ["qq/7/3", "qq/7/4"], "queued": (T0 + 2 * MINUTE).isoformat(),
         "sent": (T0 + 10 * MINUTE).isoformat()},
    ]
    assert [entry["keys"] for entry in queue.late(T0 + 6 * MINUTE, T0 + 60 * MINUTE)] == [
        ["qq/7/3", "qq/7/4"]]
    # What went out is not kept: only that it went.
    assert "one" not in (ctx.state / "alerts.json").read_text("utf-8")


def backlog(texts: list[str], language: str = "") -> str:
    """The one message of a drained backlog of alerts."""
    head = strings.t("alert_backlog", language, count=len(texts))
    return "\n\n".join([head, *texts])


def test_a_backlog_of_more_than_three_alerts_drains_as_one_message(ctx):
    queue = AlertQueue(ctx)
    for n in range(1, 6):
        queue.add(f"alert {n}", [f"qq/7/{n}"], T0 + n * MINUTE)
    sent: list[str] = []

    assert queue.drain(sent.append, T0 + 10 * MINUTE) == 5

    assert sent == [backlog([f"alert {n}" for n in range(1, 6)])]
    assert sent[0].startswith("5 alerts that could not be sent when they came:\n\nalert 1\n\n")
    assert queue.pending() == []
    assert [entry["keys"] for entry in queue.late(T0, T0 + 60 * MINUTE)] == [
        [f"qq/7/{n}"] for n in range(1, 6)]


def test_up_to_three_queued_alerts_drain_one_by_one(ctx):
    queue = AlertQueue(ctx)
    for n in range(1, COMBINE_OVER + 1):
        queue.add(f"alert {n}", [f"qq/7/{n}"], T0)
    sent: list[str] = []
    assert queue.drain(sent.append, T0 + MINUTE) == COMBINE_OVER
    assert sent == [f"alert {n}" for n in range(1, COMBINE_OVER + 1)]


def test_notices_in_a_backlog_go_out_on_their_own_and_in_order(hub, ctx):
    (hub.data / "config.toml").write_text('[chat]\nlanguage = "zh-CN"\n', "utf-8")
    queue = AlertQueue(load(hub.root))
    queue.add("notice 1", [], T0)
    for n, text in enumerate(["alert 1", "alert 2", "notice 2", "alert 3", "alert 4"], 1):
        queue.add(text, [] if text.startswith("notice") else [f"qq/7/{n}"], T0 + n * MINUTE)
    sent: list[str] = []

    assert queue.drain(sent.append, T0 + 10 * MINUTE) == 6

    # The backlog goes out where its first alert waited; a notice is never part of it.
    assert sent == [
        "notice 1", backlog(["alert 1", "alert 2", "alert 3", "alert 4"], "zh"), "notice 2"]
    assert sent[1].startswith("4 条当时未能发出的提醒：\n\nalert 1")


def test_a_backlog_message_that_fails_keeps_every_alert(ctx):
    queue = AlertQueue(ctx)
    for n in range(1, 6):
        queue.add(f"alert {n}", [f"qq/7/{n}"], T0)

    def down(text: str) -> None:
        raise ChatError("down")

    assert queue.drain(down, T0 + MINUTE) == 0
    assert [entry["text"] for entry in queue.pending()] == [f"alert {n}" for n in range(1, 6)]
    assert queue.late(T0, T0 + 60 * MINUTE) == []


# --- ticks: pokes, sweeps, heartbeat -----------------------------------------------------------


def test_first_tick_sweeps(ctx, clock):
    Health(ctx).sweep_done(T0 - MINUTE)  # a sweep of the run before: carillon missed what came
    watcher, _, spawn = rig(ctx, clock)
    calls = record_process(watcher)

    watcher.tick()

    assert calls == [(T0, ["qq", "gmail"])]
    assert spawn.times == [T0]
    assert Health(ctx).last_sweep() == T0


def test_first_run_only_sets_a_baseline(ctx, clock, fake_himalaya, fake_claude):
    fake_himalaya(server([message(1, "Card declined"), message(2, "Card declined again")]))
    fake_claude.by_subject({"declined": URGENT})
    watcher, chat, _ = rig(ctx, clock)

    watcher.tick()

    assert chat.sent == [] and fake_claude.calls() == []
    assert load_marks(ctx) == {
        "qq": Mark("7", 2, T0.isoformat()), "gmail": Mark("3", 0, T0.isoformat())}


def test_pokes_wait_for_the_batch_window_and_run_once(ctx, clock):
    watcher, _, _ = rig(ctx, clock)
    calls = record_process(watcher)
    watcher.tick()  # the first sweep
    del calls[:]

    t0 = clock.advance(60)
    poke(ctx, "qq")
    poke(ctx, "nobody")  # not a mailbox of this hub: dropped
    watcher.tick()
    clock.advance(30)
    poke(ctx, "qq")
    watcher.tick()
    clock.advance(BATCH_SECONDS - 35)
    watcher.tick()
    assert calls == []

    clock.advance(5)
    watcher.tick()
    assert calls == [(t0 + timedelta(seconds=BATCH_SECONDS), ["qq"])]
    assert list((ctx.state / "pokes").iterdir()) == []

    clock.advance(TICK_SECONDS)
    watcher.tick()
    assert len(calls) == 1


def test_sweep_every_15_minutes_without_pokes(ctx, clock):
    watcher, _, _ = rig(ctx, clock)
    calls = record_process(watcher)
    watcher.tick()
    clock.advance(SWEEP_SECONDS - TICK_SECONDS)
    watcher.tick()
    assert calls == [(T0, ["qq", "gmail"])]

    clock.advance(TICK_SECONDS)
    watcher.tick()
    assert calls[1:] == [(T0 + timedelta(seconds=SWEEP_SECONDS), ["qq", "gmail"])]

    # A batch that a sweep covered does not run again on its own.
    clock.advance(SWEEP_SECONDS - 30)
    poke(ctx, "qq")
    watcher.tick()
    clock.advance(30)
    watcher.tick()  # the sweep
    clock.advance(BATCH_SECONDS)
    watcher.tick()
    assert [names for _, names in calls] == [["qq", "gmail"]] * 3


def test_heartbeat_written_every_tick(ctx, clock):
    watcher, _, spawn = rig(ctx, clock)
    record_process(watcher)
    path = ctx.state / "watch.json"

    watcher.tick()
    assert json.loads(path.read_text("utf-8")) == {
        "pid": os.getpid(), "at": T0.isoformat(), "carillon": "up",
        "last_sweep": T0.isoformat()}

    spawn.last.exit(1)
    later = clock.advance(TICK_SECONDS)
    watcher.tick()
    assert json.loads(path.read_text("utf-8")) == {
        "pid": os.getpid(), "at": later.isoformat(), "carillon": "down",
        "last_sweep": T0.isoformat()}


# --- alerts ------------------------------------------------------------------------------------


def test_only_now_tier_alerts_and_only_once(ctx, clock, fake_himalaya, fake_claude):
    fake = fake_himalaya(server())
    fake_claude.by_subject({"declined": URGENT, "invoice": DAILY})
    watcher, chat, _ = rig(ctx, clock)
    watcher.tick()  # the baseline

    fake.write(server([
        message(1, "Card declined"), message(2, "Weekly newsletter"), message(3, "Your invoice")]))
    clock.advance(SWEEP_SECONDS)
    watcher.tick()
    assert chat.sent == [alert_of("Card declined")]
    assert VerdictStore(ctx).alerted(verdict_key("qq", "7", "1"))
    assert load_marks(ctx)["qq"].last_uid == 3

    clock.advance(SWEEP_SECONDS)
    watcher.tick()
    # A mark that went back (the watcher died before it saved one) finds the mail again: the
    # stored verdicts are reused and nothing is alerted twice.
    save_marks(ctx, {"qq": Mark("7", 0, T0.isoformat())})
    reader_calls = len(fake_claude.calls())
    clock.advance(SWEEP_SECONDS)
    watcher.tick()
    assert chat.sent == [alert_of("Card declined")]
    assert len(fake_claude.calls()) == reader_calls


def test_catch_up_after_downtime_sends_one_combined_alert(ctx, clock, fake_himalaya, fake_claude):
    fake = fake_himalaya(server())
    fake_claude.by_subject({"declined": URGENT})
    watcher, chat, _ = rig(ctx, clock)
    watcher.tick()

    fake.write(server(
        [message(1, "Card declined 1"), message(2, "Card declined 2"), message(3, "Newsletter"),
         message(4, "Card declined 3")],
        [message(1, "Card declined 4"), message(2, "Card declined 5")]))
    clock.advance(SWEEP_SECONDS)
    watcher.tick()

    assert len(chat.sent) == 1
    lines = chat.sent[0].splitlines()
    assert lines[0] == "5 urgent mails:" and len(lines) == 6
    assert [line.split(" · ")[0] for line in lines[1:]] == [
        "- [qq] Bank: Card declined 1", "- [qq] Bank: Card declined 2",
        "- [qq] Bank: Card declined 3", "- [gmail] Bank: Card declined 4",
        "- [gmail] Bank: Card declined 5"]
    store = VerdictStore(ctx)
    keys = [verdict_key("qq", "7", u) for u in "124"] + [verdict_key("gmail", "3", u) for u in "12"]
    assert all(store.alerted(key) for key in keys)

    clock.advance(SWEEP_SECONDS)
    watcher.tick()
    assert len(chat.sent) == 1


def test_up_to_three_urgent_mails_are_alerted_one_by_one(ctx, clock, fake_himalaya, fake_claude):
    fake = fake_himalaya(server())
    fake_claude.by_subject({"declined": URGENT})
    watcher, chat, _ = rig(ctx, clock)
    watcher.tick()

    subjects = [f"Card declined {n}" for n in range(1, COMBINE_OVER + 1)]
    fake.write(server([message(n, subject) for n, subject in enumerate(subjects, 1)]))
    clock.advance(SWEEP_SECONDS)
    watcher.tick()

    assert chat.sent == [alert_of(subject) for subject in subjects]


def test_alert_queued_when_chat_down_and_sent_later(ctx, clock, fake_himalaya, fake_claude):
    fake = fake_himalaya(server())
    fake_claude.by_subject({"declined": URGENT})
    watcher, chat, _ = rig(ctx, clock)
    watcher.tick()

    chat.down = True
    fake.write(server([message(1, "Card declined")]))
    queued = clock.advance(SWEEP_SECONDS)
    watcher.tick()
    assert chat.sent == []
    assert [entry["text"] for entry in AlertQueue(ctx).pending()] == [alert_of("Card declined")]
    assert VerdictStore(ctx).alerted(verdict_key("qq", "7", "1"))  # it is never sent twice
    assert load_marks(ctx)["qq"].last_uid == 1

    tries = chat.tries
    clock.advance(TICK_SECONDS)
    watcher.tick()
    assert chat.tries == tries  # a chat that just failed is not asked every tick

    chat.down = False
    clock.advance(60)
    watcher.tick()
    assert chat.sent == [alert_of("Card declined")]
    assert AlertQueue(ctx).pending() == []
    assert AlertQueue(ctx).late(T0, clock.at + MINUTE) == [{
        "keys": [verdict_key("qq", "7", "1")], "queued": queued.isoformat(),
        "sent": clock.at.isoformat()}]

    clock.advance(SWEEP_SECONDS)
    watcher.tick()
    assert len(chat.sent) == 1


def test_alerts_queued_during_an_outage_drain_as_one_message(
        ctx, clock, fake_himalaya, fake_claude):
    fake = fake_himalaya(server())
    fake_claude.by_subject({"declined": URGENT})
    watcher, chat, _ = rig(ctx, clock)
    watcher.tick()

    chat.down = True
    mails: list[dict] = []
    for n in range(1, 6):  # an urgent mail at each of five sweeps
        mails.append(message(n, f"Card declined {n}"))
        fake.write(server(mails))
        clock.advance(SWEEP_SECONDS)
        watcher.tick()
    assert chat.sent == [] and len(AlertQueue(ctx).pending()) == 5

    chat.down = False
    clock.advance(60)
    watcher.tick()

    assert chat.sent == [backlog([alert_of(f"Card declined {n}") for n in range(1, 6)])]
    assert AlertQueue(ctx).pending() == []


def test_a_new_alert_waits_behind_the_queued_ones(ctx, clock, fake_himalaya, fake_claude):
    fake = fake_himalaya(server())
    fake_claude.by_subject({"declined": URGENT})
    watcher, chat, _ = rig(ctx, clock)
    watcher.tick()

    chat.down = True
    fake.write(server([message(1, "Card declined 1")]))
    clock.advance(SWEEP_SECONDS)
    watcher.tick()
    chat.down = False
    fake.write(server([message(1, "Card declined 1"), message(2, "Card declined 2")]))
    clock.advance(SWEEP_SECONDS)
    watcher.tick()

    assert chat.sent == [alert_of("Card declined 1"), alert_of("Card declined 2")]


# --- failures ----------------------------------------------------------------------------------


def test_auth_failure_notified_once_and_cleared_on_recovery(ctx, clock, fake_himalaya):
    fake = fake_himalaya(server(fail={"qq": "authentication failed"}))
    watcher, chat, _ = rig(ctx, clock)
    notice = strings.t(
        "login_failed", "", mailbox="qq", fix="uv run apps/mail/scripts/mx.py secret-set qq")

    watcher.tick()
    assert chat.sent == [notice]
    clock.advance(SWEEP_SECONDS)
    watcher.tick()
    assert chat.sent == [notice]

    fake.write(server())  # signed in again
    clock.advance(SWEEP_SECONDS)
    watcher.tick()
    assert chat.sent == [notice]  # nothing is said about the recovery
    assert Health(ctx).problems(T0, clock.at)[0]["until"] == clock.at.isoformat()

    fake.write(server(fail={"qq": "authentication failed"}))
    clock.advance(SWEEP_SECONDS)
    watcher.tick()
    assert chat.sent == [notice, notice]  # a new spell


def test_connect_failure_notified_after_30_minutes_once(ctx, clock, fake_himalaya):
    fake_himalaya(server(fail={"qq": "connection refused"}))
    watcher, chat, _ = rig(ctx, clock)

    watcher.tick()
    clock.advance(SWEEP_SECONDS)
    watcher.tick()
    assert chat.sent == []

    clock.advance(SWEEP_SECONDS)  # 30 minutes
    watcher.tick()
    assert chat.sent == [strings.t(
        "cannot_connect", "", mailbox="qq", minutes=30, why="connection refused")]

    clock.advance(SWEEP_SECONDS)
    watcher.tick()
    assert len(chat.sent) == 1


@pytest.mark.parametrize("error, kind", [
    (ConnectError("himalaya did not answer within 90 s"), "connect"),
    (AuthError("authentication failed"), "auth"),
])
def test_a_failure_while_sorting_is_one_spell_and_one_notice(
        ctx, clock, fake_himalaya, fake_claude, monkeypatch, error, kind):
    # `fresh` gets through, then fetching the bodies fails, at every sweep.
    fake = fake_himalaya(server())
    fake_claude.by_subject({"declined": URGENT})
    watcher, chat, _ = rig(ctx, clock)
    watcher.tick()
    fake.write(server([message(1, "Card declined")]))
    real = watch.triage

    def failing(*args, **kwargs):
        raise error

    monkeypatch.setattr(watch, "triage", failing)
    if kind == "connect":
        notice = strings.t("cannot_connect", "", mailbox="qq", minutes=30, why=str(error))
    else:
        notice = strings.t(
            "login_failed", "", mailbox="qq", fix="uv run apps/mail/scripts/mx.py secret-set qq")
    start = clock.advance(SWEEP_SECONDS)
    watcher.tick()
    clock.advance(SWEEP_SECONDS)
    watcher.tick()
    assert chat.sent == ([notice] if kind == "auth" else [])  # connect: not before GRACE
    for _ in range(6):  # two hours of sweeps in all
        clock.advance(SWEEP_SECONDS)
        watcher.tick()

    assert chat.sent == [notice]
    assert Health(ctx).problems(T0, clock.at) == [
        {"mailbox": "qq", "kind": kind, "since": start.isoformat(), "until": None}]
    assert load_marks(ctx)["qq"].last_uid == 0  # the mail waits until it can be sorted

    monkeypatch.setattr(watch, "triage", real)
    clock.advance(SWEEP_SECONDS)
    watcher.tick()
    assert chat.sent == [notice, alert_of("Card declined")]  # the recovery itself says nothing
    assert Health(ctx).problems(T0, clock.at)[0]["until"] == clock.at.isoformat()


def test_a_notice_waits_for_the_chat_like_an_alert(ctx, clock, fake_himalaya):
    fake_himalaya(server(fail={"qq": "authentication failed"}))
    watcher, chat, _ = rig(ctx, clock)
    chat.down = True
    watcher.tick()
    assert chat.sent == [] and len(AlertQueue(ctx).pending()) == 1

    chat.down = False
    clock.advance(SWEEP_SECONDS)
    watcher.tick()
    assert chat.sent == [strings.t(
        "login_failed", "", mailbox="qq", fix="uv run apps/mail/scripts/mx.py secret-set qq")]


def test_reader_failure_keeps_the_mark_and_retries(ctx, clock, fake_himalaya, fake_claude):
    fake = fake_himalaya(server())
    fake_claude.by_subject({"declined": URGENT})
    watcher, chat, _ = rig(ctx, clock)
    watcher.tick()
    baseline = load_marks(ctx)["qq"]

    fake.write(server([message(1, "Card declined")]))
    fake_claude.mode("error")  # claude is not logged in: the reader does not answer
    clock.advance(SWEEP_SECONDS)
    watcher.tick()
    assert len(chat.sent) == 1
    assert chat.sent[0].startswith("Mail: the new mail in qq could not be sorted")
    assert "mx.py reader-check" in chat.sent[0]
    assert load_marks(ctx)["qq"] == baseline  # the next sweep tries again

    clock.advance(SWEEP_SECONDS)
    watcher.tick()  # still down: the same spell, nothing more is said
    assert len(chat.sent) == 1

    fake_claude.mode("")
    clock.advance(SWEEP_SECONDS)
    watcher.tick()
    assert chat.sent[1:] == [alert_of("Card declined")]
    assert load_marks(ctx)["qq"].last_uid == 1
    assert Health(ctx).problems(T0, clock.at)[0]["until"] == clock.at.isoformat()


def test_a_sweep_where_every_mail_fell_back_is_a_reader_failure(
        ctx, clock, fake_himalaya, fake_claude):
    fake = fake_himalaya(server())
    fake_claude.by_subject({"odd": BOGUS, "declined": URGENT})
    watcher, chat, _ = rig(ctx, clock)
    watcher.tick()
    mails = [message(1, "Odd 1"), message(2, "Odd 2")]

    def sweep(*more: dict) -> None:
        mails.extend(more)
        fake.write(server(mails))
        clock.advance(SWEEP_SECONDS)
        watcher.tick()

    sweep()
    unsorted = strings.t("reader_unsorted", "", mailbox="qq", count=2)
    assert chat.sent == [unsorted]
    assert load_marks(ctx)["qq"].last_uid == 2  # fallbacks are handled: the mark moves on

    sweep(message(3, "Odd 3"))
    assert chat.sent == [unsorted]  # the same spell
    sweep(message(4, "Card declined"))  # a real verdict: the reader works, the spell ends
    assert chat.sent == [unsorted, alert_of("Card declined")]
    sweep(message(5, "Odd 5"))
    assert chat.sent[2:] == [strings.t("reader_unsorted", "", mailbox="qq", count=1)]


def test_mail_left_by_the_budget_keeps_the_mark(ctx, clock, fake_himalaya, fake_claude,
                                                monkeypatch):
    fake = fake_himalaya(server())
    fake_claude.by_subject({"declined": URGENT})
    watcher, chat, _ = rig(ctx, clock)
    watcher.tick()
    baseline = load_marks(ctx)["qq"]
    fake.write(server([message(1, "Card declined 1"), message(2, "Card declined 2")]))
    real = watch.triage

    def out_of_time(ctx_, hima, reader, items, *, budget):
        assert budget == watch.TRIAGE_BUDGET == 600
        done = real(ctx_, hima, reader, items[:1], budget=budget)
        return TriageResult(done.verdicts, [key for key, *_ in items[1:]], done.fallbacks)

    monkeypatch.setattr(watch, "triage", out_of_time)
    clock.advance(SWEEP_SECONDS)
    watcher.tick()
    assert chat.sent == [alert_of("Card declined 1")]
    assert load_marks(ctx)["qq"] == baseline

    monkeypatch.setattr(watch, "triage", real)
    clock.advance(SWEEP_SECONDS)
    watcher.tick()
    assert chat.sent == [alert_of("Card declined 1"), alert_of("Card declined 2")]
    assert load_marks(ctx)["qq"].last_uid == 2


def test_no_state_lock_is_held_across_a_triage(ctx, clock, fake_himalaya, fake_claude,
                                               monkeypatch):
    fake = fake_himalaya(server())
    fake_claude.by_subject({"declined": URGENT})
    watcher, chat, _ = rig(ctx, clock)
    watcher.tick()
    fake.write(server([message(1, "Card declined")]))
    real, held = watch.triage, []

    def checked(*args, **kwargs):
        held.append((ctx.state / "lock").exists())
        return real(*args, **kwargs)

    monkeypatch.setattr(watch, "triage", checked)
    clock.advance(SWEEP_SECONDS)
    watcher.tick()
    assert held == [False] and chat.sent == [alert_of("Card declined")]


# --- carillon ----------------------------------------------------------------------------------


def test_carillon_restarted_with_backoff_and_downtime_recorded(ctx, clock):
    watcher, _, spawn = rig(ctx, clock)
    record_process(watcher)
    assert RESTART_BACKOFF == (5, 30, 120, 300)

    def at(seconds: int) -> None:
        clock.at = T0 + timedelta(seconds=seconds)
        watcher.tick()

    at(0)
    for death, restart in ((5, 10), (15, 45), (50, 170), (175, 475), (480, 780)):
        spawn.last.exit(1)
        at(death)  # seen dead: started again after the next step of the backoff
        at(restart - 1)
        at(restart)
    assert spawn.times == [T0 + timedelta(seconds=s) for s in (0, 10, 45, 170, 475, 780)]

    at(780 + STABLE_SECONDS)  # it has kept running: up since it started
    assert Health(ctx).downtime(T0 - GRACE, T0 + 2 * GRACE) == timedelta(seconds=775)

    spawn.last.exit(1)  # a stable carillon that dies starts the backoff over
    at(900)
    at(905)
    assert spawn.times[-1] == T0 + timedelta(seconds=905)


def test_carillon_down_notice_after_30_minutes_once(ctx, clock):
    watcher, chat, spawn = rig(ctx, clock)
    record_process(watcher)
    spawn.error = OSError("carillon: no such file")
    notice = strings.t(
        "carillon_down", "", minutes=30, every=15, log=str(ctx.state / "watch.log"))

    watcher.tick()
    clock.at = T0 + GRACE
    watcher.tick()
    assert chat.sent == []

    clock.advance(TICK_SECONDS)
    watcher.tick()
    assert chat.sent == [notice]
    clock.at = T0 + 3 * GRACE
    watcher.tick()
    assert chat.sent == [notice]
    assert len(spawn.times) >= 3  # it is still started again and again


def test_a_watcher_that_stopped_without_a_word_counts_as_downtime(ctx, clock):
    beat = {"pid": 1, "at": (T0 - 10 * MINUTE).isoformat(), "carillon": "up", "last_sweep": None}
    (ctx.state / "watch.json").write_text(json.dumps(beat), "utf-8")
    watcher, _, _ = rig(ctx, clock)
    record_process(watcher)

    watcher.tick()
    clock.advance(STABLE_SECONDS)
    watcher.tick()

    assert Health(ctx).downtime(T0 - GRACE, T0 + GRACE) == 10 * MINUTE


def test_run_ticks_until_ctrl_c_then_stops_carillon(ctx, clock):
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)
        if len(sleeps) == 3:
            raise KeyboardInterrupt

    watcher, _, spawn = rig(ctx, clock, sleep=sleep)
    calls = record_process(watcher)
    watcher.run()

    assert sleeps == [TICK_SECONDS] * 3 and len(calls) == 1
    assert spawn.made[0].terminated and spawn.made[0].poll() is not None
    # A watcher that stopped when it was told leaves no heartbeat behind.
    assert not (ctx.state / "watch.json").exists()


def test_sigterm_stops_carillon_at_once(ctx, clock):
    before = signal.getsignal(signal.SIGTERM)
    watcher: Watcher

    def sleep(seconds: float) -> None:
        assert signal.getsignal(signal.SIGTERM) == watcher._on_signal  # else it would kill us
        signal.raise_signal(signal.SIGTERM)
        time.sleep(0.5)  # the handler runs at the next bytecode of the main thread

    watcher, _, spawn = rig(ctx, clock, sleep=sleep)
    record_process(watcher)
    watcher.run()

    assert spawn.made[0].terminated >= 1
    assert signal.getsignal(signal.SIGTERM) == before


def test_run_stops_carillon_when_a_tick_fails(ctx, clock):
    watcher, _, spawn = rig(ctx, clock)

    def broken(names):
        raise RuntimeError("a bug")

    watcher.process = broken
    with pytest.raises(RuntimeError):
        watcher.run()
    assert spawn.made[0].terminated


def test_spawn_starts_carillon_in_the_state_directory_and_its_hook_pokes(
        ctx, make_exe, tmp_path, monkeypatch):
    monkeypatch.delenv("FAKE_CARILLON_MODE", raising=False)
    make_exe("uv", FAKE_UV)
    folder = tmp_path / "fake-carillon"
    folder.mkdir()
    fake = FakeCarillon(make_exe("carillon", carillon_body(folder / "calls.jsonl")),
                        folder / "calls.jsonl")
    write_configs(ctx)
    pokes = [ctx.state / "pokes" / name for name in ("qq", "gmail")]

    hooked = [{"hook": "qq", "exit": 0}, {"hook": "gmail", "exit": 0}]

    proc = watch.spawn_carillon(ctx)
    try:
        deadline = time.monotonic() + 60
        while not all(entry in fake.entries() for entry in hooked):
            assert proc.poll() is None, (ctx.state / "watch.log").read_text("utf-8")
            assert time.monotonic() < deadline, fake.entries()
            time.sleep(0.1)
    finally:
        proc.terminate()
        code = proc.wait(timeout=30)

    entries = fake.entries()
    assert [entry for entry in entries if "crashed" in entry] == []
    call = entries[0]
    assert call["argv"][1:] == ["-c", "carillon.toml", "--log-level", "warn", "watch"]
    assert os.path.samefile(call["cwd"], ctx.state) and call["stdin_closed"] is True
    assert all(path.is_file() for path in pokes)  # the generated hook command ran `mx hook`
    log = (ctx.state / "watch.log").read_text("utf-8")
    assert "fake carillon: out" in log and "fake carillon: err" in log
    assert proc.poll() is not None
    if os.name == "posix":  # Windows has no SIGTERM to send: carillon is killed there
        assert code == 0 and {"stopped": "SIGTERM"} in entries


def test_the_log_takes_lines_after_carillon_output(ctx, make_exe, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CARILLON_MODE", "exit")
    folder = tmp_path / "fake-carillon"
    folder.mkdir()
    make_exe("carillon", carillon_body(folder / "calls.jsonl"))
    log = watch.file_log(ctx.state / "watch.log")
    log("before")
    proc = watch.spawn_carillon(ctx)
    assert proc.wait(timeout=30) == 1
    log("after")

    lines = (ctx.state / "watch.log").read_text("utf-8").splitlines()
    assert lines[0].endswith("mx watch: before") and lines[-1].endswith("mx watch: after")
    assert "fake carillon: out" in lines and "fake carillon: err" in lines


def test_spawn_without_carillon_is_a_mail_error(ctx, monkeypatch):
    monkeypatch.setattr(watch.shutil, "which", lambda name: None)
    with pytest.raises(MailError, match="install-tool carillon"):
        watch.spawn_carillon(ctx)


# --- the commands ------------------------------------------------------------------------------


def test_watch_refuses_when_off_or_on_another_host(hub, monkeypatch, capsys):
    # The refusal ends the command with status 0: a service manager restarts a failure only, so
    # an installed service whose alerts are off or meant for another machine stops for good.
    started = []
    monkeypatch.setattr(watch, "spawn_carillon", lambda ctx: started.append("carillon"))
    monkeypatch.setattr(watch, "Watcher", lambda *args, **kwargs: started.append("watcher"))
    monkeypatch.setattr(socket, "gethostname", lambda: "here")
    assert main(["--hub", str(hub.root), "watch"]) == 0
    printed = capsys.readouterr()
    assert (printed.out, printed.err) == ("", "mx: alerts are off: mx alerts on\n")

    config = hub.data / "config.toml"
    here = this_machine(hub.state)
    for other in ("there#0a1b2c3d", "here", "here#ffffffff"):  # the same name is not enough
        config.write_text(f'[alerts]\nenabled = true\nhost = "{other}"\n', "utf-8")
        assert main(["--hub", str(hub.root), "watch"]) == 0
        printed = capsys.readouterr()
        assert (printed.out, printed.err) == (
            "", f"mx: alerts run on {other}, not on this machine ({here})\n")
    assert started == []  # no watcher, no carillon
    assert not (hub.state / "carillon.toml").exists()  # nothing was written
    assert not (hub.state / "watch.json").exists() and not (hub.state / "watch.lock").exists()

    config.write_text(f'[alerts]\nenabled = true\nhost = "{here}"\n', "utf-8")
    watch.check_here(load(hub.root))  # this machine is the one


def test_watch_refuses_without_a_console(hub, monkeypatch):
    monkeypatch.setattr(watch, "Watcher", lambda *args, **kwargs: pytest.fail("started"))
    monkeypatch.setattr(sys, "stderr", None)  # a windowless Python has none

    assert main(["--hub", str(hub.root), "watch"]) == 0


def test_one_watcher_per_hub(hub):
    hub.state.mkdir(parents=True, exist_ok=True)
    running = watch.single_instance(hub.state)
    with running, pytest.raises(MailError, match="already running"), watch.single_instance(
            hub.state):
        pass
    with watch.single_instance(hub.state):  # free again once the first one stopped
        pass


def test_hook_only_touches_a_file(hub, fake_himalaya, fake_claude, capsys):
    fake = fake_himalaya(server())

    # carillon passes nothing more today; whatever it adds one day is ignored
    assert main(["--hub", str(hub.root), "hook", "qq", "--from", "carillon", "x"]) == 0

    assert (hub.state / "pokes" / "qq").is_file()
    assert capsys.readouterr().out == ""
    assert fake.calls() == [] and fake_claude.calls() == []
    assert {path.name for path in hub.state.iterdir()} == {"himalaya.toml", "pokes"}

    assert main(["--hub", str(hub.root), "hook", "../qq"]) == 1  # a name is a file name
    assert "not a mailbox name" in capsys.readouterr().err
    assert [path.name for path in (hub.state / "pokes").iterdir()] == ["qq"]


# --- settings that change while the watcher runs -----------------------------------------------

WORK = '\n[[mailbox]]\nname = "work"\naddress = "me@work.example"\nprovider = "gmail"\n'
QQ_ONLY = BOXES.split("\n[[mailbox]]\nname = \"gmail\"")[0]


def alerts_here(hub, language: str = "", model: str = "sonnet") -> None:
    """config.toml with alerts on for this machine, and this language and reader model."""
    hub.state.mkdir(parents=True, exist_ok=True)
    (hub.data / "config.toml").write_text(
        f'[alerts]\nenabled = true\nhost = "{this_machine(hub.state)}"\n\n'
        f'[reader]\nmodel = "{model}"\n\n[chat]\nlanguage = "{language}"\n', "utf-8", newline="\n")


@pytest.fixture
def live(hub, clock):
    """The hub with the mailboxes qq and gmail, alerts on for this machine, at T0."""
    write_mailboxes(hub, BOXES)
    alerts_here(hub)
    return load(hub.root)


def work_inbox(*messages: dict) -> dict:
    return {"INBOX": {"uidvalidity": "5", "messages": list(messages)}}


def test_a_new_mailbox_starts_from_a_baseline_and_a_removed_one_is_left(
        live, hub, clock, fake_himalaya, fake_claude):
    fake = fake_himalaya(server(work=work_inbox(message(1, "Card declined before"))))
    fake_claude.by_subject({"declined": URGENT})
    watcher, chat, spawn = rig(live, clock)
    watcher.tick()  # the baseline of qq and gmail
    save_marks(live, {"work": Mark("5", 0, (T0 - 60 * MINUTE).isoformat())})  # of an old setup
    Health(live).mailbox_failed("gmail", "auth", "refused", T0)

    write_mailboxes(hub, QQ_ONLY + WORK)
    reloaded = clock.advance(TICK_SECONDS)
    watcher.tick()

    # carillon runs again, with a config of the mailboxes there are now
    assert len(spawn.made) == 2 and spawn.made[0].terminated and spawn.last.poll() is None
    carillon = tomllib.loads((hub.state / "carillon.toml").read_text("utf-8"))
    assert set(carillon["accounts"]) == {"qq", "work"}
    # work starts from a baseline at once: the mail there is not news, an old mark is not used
    assert load_marks(live)["work"] == Mark("5", 1, reloaded.isoformat())
    assert chat.sent == []
    # a problem of the mailbox that is gone does not go on for ever
    assert Health(live).problems(T0, reloaded)[0]["until"] == reloaded.isoformat()

    fake.write(server(
        gmail=[message(1, "Card declined in gmail")],
        work=work_inbox(message(1, "Card declined before"), message(2, "Card declined at work"))))
    clock.advance(SWEEP_SECONDS)
    watcher.tick()

    assert chat.sent == [alert_of("Card declined at work", mailbox="work")]
    assert {call["argv"][4] for call in fake.calls()} >= {"work"}
    assert load_marks(live)["gmail"].last_uid == 0  # gmail is no longer looked at


def test_a_poke_of_a_mailbox_that_comes_with_a_reload_is_not_lost(live, hub, clock):
    watcher, _, _ = rig(live, clock)
    calls = record_process(watcher)
    watcher.tick()
    del calls[:]

    # A carillon that read the new list before the watcher did pokes for the new mailbox.
    write_mailboxes(hub, BOXES + WORK)
    poke(live, "work")
    reloaded = clock.advance(TICK_SECONDS)
    watcher.tick()
    assert calls == [(reloaded, ["work"])]  # its first sweep, at once

    poke(live, "work")
    poked = clock.advance(TICK_SECONDS)
    watcher.tick()
    clock.advance(BATCH_SECONDS)
    watcher.tick()
    assert calls[1:] == [(poked + timedelta(seconds=BATCH_SECONDS), ["work"])]


def test_a_new_language_and_reader_model_apply_without_a_restart(
        live, hub, clock, fake_himalaya, fake_claude):
    fake = fake_himalaya(server())
    fake_claude.by_subject({"declined": URGENT})
    watcher, chat, spawn = rig(live, clock)
    watcher.tick()

    alerts_here(hub, language="zh-CN", model="haiku")
    fake.write(server([message(1, "Card declined")], fail={"gmail": "authentication failed"}))
    clock.advance(SWEEP_SECONDS)
    watcher.tick()

    assert len(spawn.made) == 1  # the mailboxes are the same: carillon goes on
    argv = fake_claude.calls()[-1]["argv"]
    assert argv[argv.index("--model") + 1] == "haiku"
    assert "in zh-CN" in argv[argv.index("--system-prompt") + 1]
    assert chat.sent == [
        strings.t("login_failed", "zh-CN", mailbox="gmail",
                  fix="uv run apps/mail/scripts/mx.py secret-set gmail"),
        alert_text(verdict(subject="Card declined", amount=None, deadline=None, phishing=False),
                   "zh-CN")]


@pytest.mark.parametrize("switch", [
    "[alerts]\nenabled = false\nhost = \"\"\n",
    "[alerts]\nenabled = true\nhost = \"office#0a1b2c3d\"\n",
])
def test_alerts_switched_off_or_moved_stop_the_watcher(live, hub, clock, switch):
    sleeps: list[float] = []
    lines: list[str] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)
        if len(sleeps) == 2:
            (hub.data / "config.toml").write_text(switch, "utf-8", newline="\n")
        assert len(sleeps) < 5, "the watcher did not stop"

    watcher, _, spawn = rig(live, clock, sleep=sleep, log=lines.append)
    record_process(watcher)
    watcher.run()  # it returns: the service ends with status 0 and is not started again

    assert len(sleeps) == 2
    assert spawn.made[0].terminated and spawn.made[0].poll() is not None
    assert not (hub.state / "watch.json").exists()  # a stop that was asked for is no downtime
    assert any(line.startswith("stopped: alerts ") for line in lines), lines


def test_settings_that_cannot_be_read_leave_the_old_ones(live, hub, clock):
    lines: list[str] = []
    watcher, _, spawn = rig(live, clock, log=lines.append)
    record_process(watcher)
    watcher.tick()

    (hub.data / "mailboxes.toml").write_text("[[mailbox]\nname = ", "utf-8", newline="\n")
    for _ in range(3):
        clock.advance(TICK_SECONDS)
        watcher.tick()
    assert len([line for line in lines if "cannot be read" in line]) == 1, lines
    assert list(watcher.ctx.mailboxes) == ["qq", "gmail"] and len(spawn.made) == 1

    write_mailboxes(hub, BOXES + WORK)
    clock.advance(TICK_SECONDS)
    watcher.tick()
    assert list(watcher.ctx.mailboxes) == ["qq", "gmail", "work"] and len(spawn.made) == 2


def test_a_tick_whose_state_write_fails_is_logged_and_the_watcher_goes_on(ctx, clock):
    sleeps: list[float] = []
    lines: list[str] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)
        if len(sleeps) == 3:
            raise KeyboardInterrupt

    watcher, _, _ = rig(ctx, clock, sleep=sleep, log=lines.append)
    calls: list[list[str]] = []

    def process(names: list[str]) -> None:
        calls.append(list(names))
        if len(calls) == 1:
            raise PermissionError(13, "Access is denied", str(ctx.state / "marks.json"))

    watcher.process = process
    watcher.run()

    assert len(sleeps) == 3  # it kept running after the failed write
    assert any(line.startswith("a tick failed: ") and "Access is denied" in line
               for line in lines), lines


# --- the tools the watcher runs ----------------------------------------------------------------

INSTALL = "uv run apps/mail/scripts/mx.py install-tool {}"


def only_tools(tmp_path, monkeypatch, make_exe, **versions: str | None) -> None:
    """PATH is the folder of make_exe and nothing else (the fake cc-connect is there too), with a
    tool for each of `versions` that prints that version; None removes the tool."""
    folder = tmp_path / "bin"
    folder.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(folder))
    if not (folder / "uv.py").exists():
        make_exe("uv", FAKE_UV)  # the generated configs name it
    for name, version in versions.items():
        for path in folder.glob(f"{name}.*"):
            path.unlink()
        if (folder / name).exists():
            (folder / name).unlink()
        if version is not None:
            make_exe(name, f"print('{name} v{version} +imap')")


@pytest.fixture
def started(monkeypatch) -> list[str]:
    """`mx watch` with a Watcher that records that it was started, and nothing more."""
    found: list[str] = []

    class Started:
        def __init__(self, *args, **kwargs):
            found.append("watcher")

        def run(self) -> None:
            pass

    monkeypatch.setattr(watch, "Watcher", Started)
    return found


def test_watch_stops_and_tells_the_chat_once_when_a_tool_is_not_the_pinned_one(
        hub, make_exe, fake_chat, tmp_path, monkeypatch, capsys, started):
    write_mailboxes(hub, BOXES)
    alerts_here(hub)
    record_chat(hub)
    only_tools(tmp_path, monkeypatch, make_exe, himalaya="1.1.0", carillon="0.2.0")
    other = strings.t("tool_other", "", tool="himalaya", found="1.1.0", pinned="2.2.1")

    assert main(["--hub", str(hub.root), "watch"]) == 0

    assert started == []
    assert fake_chat.messages() == [strings.t(
        "tools_stopped", "", problems=other, commands=INSTALL.format("himalaya"))]
    err = capsys.readouterr().err
    assert "mx: himalaya on PATH (" in err and "is 1.1.0, but the mail app needs 2.2.1" in err
    assert "is 1.1.0, but the mail app needs 2.2.1" in (hub.state / "watch.log").read_text("utf-8")

    assert main(["--hub", str(hub.root), "watch"]) == 0  # the service starts it again: no news
    assert len(fake_chat.messages()) == 1

    only_tools(tmp_path, monkeypatch, make_exe, carillon=None)
    assert main(["--hub", str(hub.root), "watch"]) == 0  # a new problem: told, with the old one
    missing = strings.t("tool_missing", "", tool="carillon", pinned="0.2.0")
    assert fake_chat.messages()[1:] == [strings.t(
        "tools_stopped", "", problems=f"{other}; {missing}",
        commands=f"{INSTALL.format('himalaya')}\n{INSTALL.format('carillon')}")]

    only_tools(tmp_path, monkeypatch, make_exe, himalaya="2.2.1", carillon="0.2.0")
    assert main(["--hub", str(hub.root), "watch"]) == 0
    assert started == ["watcher"] and len(fake_chat.messages()) == 2

    only_tools(tmp_path, monkeypatch, make_exe, himalaya="1.1.0")  # broken again: told again
    assert main(["--hub", str(hub.root), "watch"]) == 0
    assert len(fake_chat.messages()) == 3 and started == ["watcher"]
    capsys.readouterr()


def test_a_tool_notice_the_chat_did_not_take_is_tried_at_the_next_start(
        hub, make_exe, fake_chat, tmp_path, monkeypatch, capsys, started):
    write_mailboxes(hub, BOXES)
    alerts_here(hub, language="zh-CN")
    record_chat(hub)
    only_tools(tmp_path, monkeypatch, make_exe, himalaya="2.2.1", carillon="0.1.0")
    fake_chat.mode("fail")

    assert main(["--hub", str(hub.root), "watch"]) == 0
    assert fake_chat.messages() == [] and started == []

    fake_chat.mode("")
    assert main(["--hub", str(hub.root), "watch"]) == 0
    assert fake_chat.messages() == [strings.t(
        "tools_stopped", "zh-CN",
        problems=strings.t("tool_other", "zh-CN", tool="carillon", found="0.1.0", pinned="0.2.0"),
        commands=INSTALL.format("carillon"))]
    assert fake_chat.messages()[0].startswith("邮件：")
    capsys.readouterr()

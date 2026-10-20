"""The report sections (mailkit.report, `mx report daily|monthly`, mail/reports/*.md).

The server is the fake himalaya and the reader the fake claude (fakes.py). The stores tell the
time by `mailkit.clock.now`, which the `clock` fixture holds at T0 unless a test moves it; a daily
run covers [SINCE, T0) unless a test says otherwise.
"""

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
from mailkit import clock as clock_module
from mailkit import report
from mailkit.alerts import AlertQueue
from mailkit.cli import main
from mailkit.context import load
from mailkit.health import Health, sign_in_command
from mailkit.himalaya import Himalaya, TimedOut
from mailkit.paths import this_machine
from mailkit.reader import Reader, Verdict
from mailkit.strings import STRINGS, t
from mailkit.tools import TOOLS
from mailkit.verdicts import VerdictStore
from mailtest import ROOT, write_mailboxes

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
SINCE = T0 - timedelta(days=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
SECOND = timedelta(seconds=1)

# What the fake claude answers for a mail whose subject holds the keyword (fakes.py).
SORTED = {
    "friday": {
        "summary": "Alice asks whether Friday works.", "tier": "daily",
        "category": "awaiting_reply", "deadline": "2026-10-23"},
    "renewal": {
        "summary": "The domain example.org expires soon.", "tier": "daily",
        "category": "due_soon", "deadline": "2026-10-30"},
    "parcel": {"summary": "Your parcel is on its way.", "tier": "daily", "category": "orders"},
    "bill": {
        "summary": "The October power bill.", "tier": "daily", "category": "bills",
        "amount": "86.20 CNY"},
    "payment": {"summary": "A card payment failed.", "tier": "daily", "category": "money"},
    "login": {
        "summary": "A sign-in from a new device.", "tier": "daily", "category": "security",
        "phishing": True},
    "declined": {
        "summary": "A card payment was declined.", "tier": "now", "category": "money",
        "amount": "128.00 CNY"},
    "locked": {"summary": "The account was locked.", "tier": "now", "category": "security"},
    "note": {"summary": "A note.", "tier": "daily", "category": "other"},
}


# The dates of SORTED, written in every mail: a deadline the sender did not write is dropped.
DUE = "Friday is 2026-10-23; the renewal is due on 2026-10-30."


def eml(subject: str, when: datetime, sender: str = "Shop <shop@example.org>",
        body: str | None = None) -> str:
    return (
        f"From: {sender}\nTo: me@qq.com\nSubject: {subject}\nDate: {format_datetime(when)}\n"
        f"Content-Type: text/plain; charset=utf-8\n\n{body or f'The text of {subject}. {DUE}'}\n")


def message(uid: int, subject: str, when: datetime = T0 - HOUR, **fields) -> dict:
    return {
        "uid": uid, "date": when.isoformat(), "internal_date": when.isoformat(), "seen": False,
        "raw": eml(subject, when, **fields)}


def server(qq=(), gmail=(), **extra) -> dict:
    """The inboxes of qq (UIDVALIDITY 7) and gmail (3)."""
    return {
        "qq": {"INBOX": {"uidvalidity": "7", "messages": list(qq)}},
        "gmail": {"INBOX": {"uidvalidity": "3", "messages": list(gmail)}},
        **extra}


class Clock:
    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def clock(monkeypatch) -> Clock:
    fake = Clock(T0)
    monkeypatch.setattr(clock_module, "now", fake)
    return fake


@pytest.fixture
def ctx(hub, clock):
    """The hub with the mailboxes qq and gmail, alerts off."""
    write_mailboxes(hub, BOXES)
    return load(hub.root)


def alerts_on(hub) -> None:
    hub.state.mkdir(parents=True, exist_ok=True)
    (hub.data / "config.toml").write_text(
        f'[alerts]\nenabled = true\nhost = "{this_machine(hub.state)}"\n', "utf-8",
        newline="\n")


def beat(ctx, at: datetime = T0) -> None:
    """The heartbeat of a watcher that runs."""
    (ctx.state / "watch.json").write_text(json.dumps({"at": at.isoformat()}), "utf-8")


def daily(ctx, since: datetime = SINCE, until: datetime = T0, language: str = "en",
          hima: Himalaya | None = None, **extra) -> str:
    return report.daily(
        ctx, hima or Himalaya(ctx), Reader("sonnet"), since, until, language, **extra)


class SlowFetch(Himalaya):
    """The fake himalaya, except that fetching the mail with one of the UIDs `slow` takes longer
    than himalaya's timeout (a very large mail)."""

    def __init__(self, ctx, slow: set[int]):
        super().__init__(ctx)
        self.slow = slow
        self.fetched: list[int] = []  # the UIDs asked for, call after call

    def fetch_raw(self, mailbox: str, folder: str, uids: list[int]) -> dict[int, bytes]:
        self.fetched += uids
        if self.slow & set(uids):
            raise TimedOut(f"himalaya did not answer within {report.HIMALAYA_TIMEOUT:g} s")
        return super().fetch_raw(mailbox, folder, uids)


def lines(text: str) -> list[str]:
    return text.splitlines()


# --- the daily section -------------------------------------------------------------------------


def test_daily_groups_and_lines_en_and_zh(ctx, fake_himalaya, fake_claude):
    fake_himalaya(server(
        qq=[message(1, "Friday meeting", T0 - 5 * HOUR, sender="Alice <alice@example.org>"),
            message(2, "Domain renewal", T0 - 4 * HOUR),
            message(3, "Parcel shipped", T0 - 3 * HOUR)],
        gmail=[message(4, "October bill", T0 - 5 * HOUR, sender="Power Co <bill@power.example>",
                       body="Amount due: 86.20 CNY."),
               message(5, "Payment failed", T0 - 4 * HOUR, sender="Bank <bank@example.org>"),
               message(6, "New login", T0 - 3 * HOUR),
               message(7, "Weekly news", T0 - 2 * HOUR)]))
    fake_claude.by_subject(SORTED)

    assert daily(ctx) == (
        "Waiting for your reply:\n"
        "- [qq] Alice: Alice asks whether Friday works. · 2026-10-23\n"
        "\n"
        "Due soon:\n"
        "- [qq] Shop: The domain example.org expires soon. · 2026-10-30\n"
        "\n"
        "Orders and deliveries:\n"
        "- [qq] Shop: Your parcel is on its way.\n"
        "\n"
        "Bills and receipts:\n"
        "- [gmail] Power Co: The October power bill. · 86.20 CNY\n"
        "\n"
        "Other:\n"
        "- [gmail] Bank: A card payment failed.\n"
        "- [gmail] Shop: A sign-in from a new device. · ⚠ may be phishing")
    calls = len(fake_claude.calls())

    # The verdicts are kept: the second run asks the reader nothing.
    assert daily(ctx, language="zh-CN") == (
        "等你回复：\n"
        "- [qq] Alice: Alice asks whether Friday works. · 2026-10-23\n"
        "\n"
        "快到期：\n"
        "- [qq] Shop: The domain example.org expires soon. · 2026-10-30\n"
        "\n"
        "订单和快递：\n"
        "- [qq] Shop: Your parcel is on its way.\n"
        "\n"
        "账单和收据：\n"
        "- [gmail] Power Co: The October power bill. · 86.20 CNY\n"
        "\n"
        "其他：\n"
        "- [gmail] Bank: A card payment failed.\n"
        "- [gmail] Shop: A sign-in from a new device. · ⚠ 可能是钓鱼邮件")
    assert len(fake_claude.calls()) == calls


def test_daily_alerts_off_lists_urgent_first(ctx, fake_himalaya, fake_claude):
    fake_himalaya(server(
        qq=[message(1, "Friday meeting", T0 - 5 * HOUR, sender="Alice <alice@example.org>"),
            message(2, "Card declined", T0 - 4 * HOUR, sender="Bank <bank@example.org>",
                    body="128.00 CNY was declined.")]))
    fake_claude.by_subject(SORTED)

    assert daily(ctx) == (
        "Urgent:\n"
        "- [qq] Bank: A card payment was declined. · 128.00 CNY\n"
        "\n"
        "Waiting for your reply:\n"
        "- [qq] Alice: Alice asks whether Friday works. · 2026-10-23")


def test_daily_alerts_on_leaves_out_alerted_mail(hub, clock, fake_himalaya, fake_claude):
    alerts_on(hub)
    write_mailboxes(hub, BOXES)
    ctx = load(hub.root)
    beat(ctx)
    fake_himalaya(server(
        qq=[message(1, "Card declined", T0 - 5 * HOUR, sender="Bank <bank@example.org>",
                    body="128.00 CNY was declined."),
            message(2, "Account locked", T0 - 4 * HOUR, sender="Bank <bank@example.org>")]))
    fake_claude.by_subject(SORTED)
    VerdictStore(ctx).mark_alerted("qq/7/1", T0 - 5 * HOUR)  # the watcher alerted the first
    queue = AlertQueue(ctx)
    queue.add("an alert", ["gmail/3/8"], T0 - 3 * HOUR)
    queue.drain(lambda text: None, T0 - 2 * HOUR)  # it went out an hour late
    queue.add("another alert", ["gmail/3/9"], T0 - HOUR)  # it waits
    queue.add("a notice", [], T0 - HOUR)  # a notice is no alert

    assert daily(ctx) == (
        "Urgent:\n"
        "- [qq] Bank: The account was locked.\n"
        "\n"
        "Alerts ran the whole time since the last report.\n"
        "\n"
        "Urgent mails whose alert went out late because the chat could not be reached: 1.\n"
        "\n"
        "Urgent mails whose alert still waits for the chat: 1.")


def test_daily_says_when_alerts_were_down_or_do_not_run(hub, clock, fake_himalaya):
    alerts_on(hub)
    write_mailboxes(hub, BOXES)
    ctx = load(hub.root)
    fake_himalaya(server())
    health = Health(ctx)
    health.carillon_down(T0 - 5 * HOUR)
    health.carillon_up(T0 - 2 * HOUR - 25 * timedelta(minutes=1))
    beat(ctx)

    assert lines(daily(ctx))[-1] == "Alerts were down for 2 h 35 min since the last report."
    assert lines(daily(ctx, language="zh"))[-1] == "自上次日报以来，实时提醒有 2 小时 35 分钟没有运行。"

    (ctx.state / "watch.json").unlink()  # the watcher was stopped
    assert lines(daily(ctx))[-3:] == [
        "Alerts were down for 2 h 35 min since the last report.", "",
        t("alerts_stopped", "en")]


def test_daily_nothing_is_one_line(ctx, fake_himalaya, fake_claude):
    fake_himalaya(server(qq=[message(1, "Weekly news")]))  # the fake sorts it as ignore
    assert daily(ctx) == "No new mail needs your attention."
    assert daily(ctx, language="zh") == "没有需要你关注的新邮件。"


def test_daily_lists_other_mailboxes_when_one_fails(ctx, clock, fake_himalaya, fake_claude):
    fake = fake_himalaya(server(
        qq=[message(1, "Parcel shipped")], gmail=[message(2, "October bill", body="86.20 CNY")],
        fail={"gmail": "connection refused"}))
    fake_claude.by_subject(SORTED)

    assert daily(ctx) == (
        "Orders and deliveries:\n"
        "- [qq] Shop: Your parcel is on its way.\n"
        "\n"
        "gmail could not be read (connection refused); its mail is listed once it can be read "
        "again.")

    # The next report reads gmail from where the failed one started; qq only from its own window.
    data = fake.server()
    del data["fail"]
    data["qq"]["INBOX"]["messages"].append(message(3, "Friday meeting", T0 + HOUR))
    fake.write(data)
    clock.at = T0 + DAY
    assert daily(ctx, T0, T0 + DAY) == (
        "Waiting for your reply:\n"
        "- [qq] Shop: Alice asks whether Friday works. · 2026-10-23\n"
        "\n"
        "Bills and receipts:\n"
        "- [gmail] Shop: The October power bill. · 86.20 CNY")
    # That report went out, so the one after it starts at its end for every mailbox.
    clock.at = T0 + 2 * DAY
    assert daily(ctx, T0 + DAY, T0 + 2 * DAY) == t("daily_nothing", "en")


def test_daily_login_problem_said_once(ctx, fake_himalaya, fake_claude):
    fake = fake_himalaya(server(
        qq=[message(1, "Parcel shipped")],
        fail={"qq": ["authentication failed", "invalid credentials"]}))
    fake_claude.by_subject(SORTED)
    fix = sign_in_command(ctx.mailbox("qq"))
    login = t("login_problem", "en", mailbox="qq", fix=fix)
    unread = (
        "qq could not be read (authentication failed: invalid credentials); its mail is listed "
        "once it can be read again.")

    assert lines(daily(ctx))[-1] == login
    assert fix in login
    assert lines(daily(ctx))[-1] == unread  # the same spell: the fix was told already

    data = fake.server()
    del data["fail"]
    fake.write(data)
    assert "cannot sign in" not in daily(ctx)  # it signs in again: the spell ends
    data["fail"] = {"qq": "authentication failed"}
    fake.write(data)
    assert lines(daily(ctx))[-1] == login  # a new spell

    # The watcher told the user of gmail's login problem: the report does not tell it again.
    assert Health(ctx).mailbox_failed("gmail", "auth", "refused", T0) is True
    data["fail"] = {"gmail": "authentication failed"}
    fake.write(data)
    assert lines(daily(ctx))[-1] == (
        "gmail could not be read (authentication failed); its mail is listed once it can be "
        "read again.")


def test_daily_reader_failure_is_reported_not_dropped(ctx, clock, fake_himalaya, fake_claude):
    fake_himalaya(server(
        qq=[message(1, "Parcel shipped"), message(2, "Domain renewal")],
        gmail=[message(3, "October bill", body="86.20 CNY")]))
    fake_claude.mode("error")

    assert daily(ctx) == (
        "Mail not sorted because the reader did not answer (Not logged in · Please run /login): "
        "3. The next report tries again. To test the reader, type this in the server's "
        "terminal, in the hub's folder: uv run apps/mail/scripts/mx.py reader-check")
    # The first mailbox showed that the reader is down: the second did not ask it again.
    assert len(fake_claude.calls()) <= 2

    fake_claude.mode("")
    fake_claude.by_subject(SORTED)
    clock.at = T0 + DAY
    assert daily(ctx, T0, T0 + DAY) == (
        "Due soon:\n"
        "- [qq] Shop: The domain example.org expires soon. · 2026-10-30\n"
        "\n"
        "Orders and deliveries:\n"
        "- [qq] Shop: Your parcel is on its way.\n"
        "\n"
        "Bills and receipts:\n"
        "- [gmail] Shop: The October power bill. · 86.20 CNY")


def test_daily_reuses_verdicts_and_carries_left_mail(ctx, clock, fake_himalaya, fake_claude):
    fake_himalaya(server(qq=[message(1, "Old bill"), message(2, "Parcel shipped")]))
    fake_claude.by_subject(SORTED)
    VerdictStore(ctx).put(Verdict(
        key="qq/7/1", mailbox="qq", sender="Power Co", subject="Old bill",
        date=(T0 - HOUR).isoformat(), summary="The September power bill.", code=None,
        amount=None, deadline=None, tier="daily", category="bills", reason="A bill.",
        phishing=False))
    times = iter([0.0])

    def spent() -> float:  # the budget is gone once the mailboxes are listed
        return next(times, report.DAILY_BUDGET + 1)

    assert daily(ctx, monotonic=spent) == (
        "Bills and receipts:\n"
        "- [qq] Power Co: The September power bill.\n"
        "\n"
        "Mail not sorted in time: 1. The next report lists it.")
    assert fake_claude.calls() == []

    clock.at = T0 + DAY
    assert daily(ctx, T0, T0 + DAY) == (
        "Orders and deliveries:\n"
        "- [qq] Shop: Your parcel is on its way.")
    assert len(fake_claude.calls()) == 1
    clock.at = T0 + 2 * DAY
    assert daily(ctx, T0 + DAY, T0 + 2 * DAY) == t("daily_nothing", "en")


def test_carried_mail_comes_again_until_its_report_went_out(ctx, clock, fake_himalaya,
                                                            fake_claude):
    fake_himalaya(server(qq=[message(1, "Parcel shipped")]))
    fake_claude.by_subject(SORTED)
    times = iter([0.0])
    daily(ctx, monotonic=lambda: next(times, report.DAILY_BUDGET + 1))  # the mail is left
    listed = "Orders and deliveries:\n- [qq] Shop: Your parcel is on its way."

    clock.at = T0 + DAY
    assert daily(ctx, T0, T0 + DAY) == listed
    # That report could not be sent, so cc-hub covers the same window again: so does the mail.
    clock.at = T0 + DAY + HOUR
    assert daily(ctx, T0, T0 + DAY + HOUR) == listed
    clock.at = T0 + 2 * DAY
    assert daily(ctx, T0 + DAY + HOUR, T0 + 2 * DAY) == t("daily_nothing", "en")


def test_daily_window_is_exact(ctx, fake_himalaya, fake_claude):
    since = datetime(2026, 10, 19, 5, 0, tzinfo=UTC)
    until = since + DAY
    # A server far behind UTC: the mail that arrived at `since` arrived the day before there,
    # and the server searches by that day.
    behind = timezone(timedelta(hours=-11))
    assert since.astimezone(behind).date() < since.date()
    fake_himalaya(server(qq=[
        message(1, "note 1", since - SECOND), message(2, "note 2", since.astimezone(behind)),
        message(3, "note 3", until - SECOND), message(4, "note 4", until)]))
    fake_claude.by_subject(SORTED)

    assert daily(ctx, since, until) == "Other:\n- [qq] Shop: A note.\n- [qq] Shop: A note."
    sorted_subjects = re.findall(r"^Subject: (.*)$", "".join(
        call["stdin"] for call in fake_claude.calls()), re.MULTILINE)
    assert sorted(sorted_subjects) == ["note 2", "note 3"]


def test_daily_lists_mail_it_could_not_sort_or_fetch(ctx, clock, fake_himalaya, fake_claude):
    unfetchable = message(3, "Parcel shipped")
    unfetchable["fail_fetch"] = "imap: NO [UNAVAILABLE] cannot fetch the message"
    fake = fake_himalaya(server(
        qq=[message(1, "Odd mail", sender="Bob <bob@example.org>"), message(2, "Friday")],
        gmail=[unfetchable]))
    fake_claude.by_subject({**SORTED, "odd": {"tier": "bogus"}})

    assert daily(ctx) == (
        "Waiting for your reply:\n"
        "- [qq] Shop: Alice asks whether Friday works. · 2026-10-23\n"
        "\n"
        "Could not be sorted, so have a look yourself:\n"
        "- [qq] Bob: Odd mail\n"
        "\n"
        "Mail in gmail that could not be fetched: 1. The next report tries again.")
    assert daily(ctx, language="zh").splitlines()[3:] == [
        "无法分类，请自己看一下：", "- [qq] Bob: Odd mail", "",
        "gmail 有 1 封邮件无法取回，下次日报会再试。"]

    data = fake.server()
    del data["gmail"]["INBOX"]["messages"][0]["fail_fetch"]
    fake.write(data)
    clock.at = T0 + DAY
    assert daily(ctx, T0, T0 + DAY) == (
        "Orders and deliveries:\n"
        "- [gmail] Shop: Your parcel is on its way.")


def test_a_reader_answer_in_the_words_of_a_fallback_is_an_ordinary_verdict(
        ctx, fake_himalaya, fake_claude):
    # What tells a fallback is the marker triage sets, never what the reader wrote: a mail that
    # makes the reader answer "could not be fetched" with no summary is sorted like any other.
    fake_himalaya(server(qq=[message(1, "Odd parcel"), message(2, "Odd order")]))
    fake_claude.by_subject({
        "odd parcel": {"summary": "", "tier": "daily", "category": "orders",
                       "reason": "could not be fetched"},
        "odd order": {"summary": "", "tier": "daily", "category": "orders",
                      "reason": "could not be sorted"}})

    assert daily(ctx) == (
        "Orders and deliveries:\n"
        "- [qq] Shop: Odd parcel\n"
        "- [qq] Shop: Odd order")


def test_daily_section_stays_within_cc_hubs_limit(ctx, fake_himalaya, fake_claude):
    count = 120
    store = VerdictStore(ctx)
    store.put_many(Verdict(
        key=f"qq/7/{uid}", mailbox="qq", sender="S" * 60, subject="s",
        date=(T0 - HOUR).isoformat(), summary="x" * 200, code=None, amount=None,
        deadline=None, tier="daily", category="other", reason="r", phishing=False)
        for uid in range(1, count + 1))
    fake_himalaya(server(qq=[message(uid, f"mail {uid}") for uid in range(1, count + 1)]))

    text = daily(ctx)
    assert len(text) <= report.SECTION_LIMIT
    shown = [line for line in lines(text) if line.startswith("- [qq]")]
    assert lines(text)[-1] == t("more_mail", "en", n=count - len(shown))
    assert 0 < len(shown) < count
    assert fake_claude.calls() == []


def test_daily_selects_mail_by_arrival(ctx, fake_himalaya, fake_claude):
    late = message(1, "Friday meeting", T0 - 3 * DAY)  # dated three days ago ...
    late["internal_date"] = (T0 - HOUR).isoformat()  # ... but it only arrived now
    undated = {
        "uid": 2, "internal_date": (T0 - 2 * HOUR).isoformat(), "seen": False,
        "raw": ("From: Shop <shop@example.org>\nTo: me@qq.com\nSubject: Parcel shipped\n"
                "Content-Type: text/plain; charset=utf-8\n\nThe text.\n")}
    early = message(3, "Domain renewal", T0 - HOUR)  # dated now ...
    early["internal_date"] = (SINCE - HOUR).isoformat()  # ... but it arrived before the window
    fake_himalaya(server(qq=[late, undated, early]))
    fake_claude.by_subject(SORTED)

    assert daily(ctx) == (
        "Waiting for your reply:\n"
        "- [qq] Shop: Alice asks whether Friday works. · 2026-10-23\n"
        "\n"
        "Orders and deliveries:\n"
        "- [qq] Shop: Your parcel is on its way.")


def test_nothing_line_names_only_the_mailboxes_read(ctx, fake_himalaya, fake_claude):
    fake_himalaya(server(qq=[message(1, "Weekly news")], fail={"gmail": "connection refused"}))
    unread = (
        "gmail could not be read (connection refused); its mail is listed once it can be read "
        "again.")
    assert daily(ctx) == (
        f"No new mail needs your attention in the mailboxes that could be read.\n\n{unread}")
    assert lines(daily(ctx, language="zh"))[0] == "能读取的邮箱里没有需要你关注的新邮件。"


def test_carried_mail_survives_a_rerun_that_was_stopped(ctx, clock, fake_himalaya, fake_claude,
                                                        monkeypatch):
    fake_himalaya(server(qq=[message(1, "Parcel shipped")]))
    fake_claude.by_subject(SORTED)
    times = iter([0.0])
    daily(ctx, monotonic=lambda: next(times, report.DAILY_BUDGET + 1))  # the mail is left
    listed = "Orders and deliveries:\n- [qq] Shop: Your parcel is on its way."
    clock.at = T0 + DAY
    assert daily(ctx, T0, T0 + DAY) == listed  # this report is not sent

    def stopped(*args, **kwargs):
        raise RuntimeError("stopped after 300 s")

    real = report.triage
    monkeypatch.setattr(report, "triage", stopped)
    clock.at = T0 + DAY + HOUR
    with pytest.raises(RuntimeError):  # the run over the same window is stopped halfway ...
        daily(ctx, T0, T0 + DAY + HOUR)
    monkeypatch.setattr(report, "triage", real)

    # ... and that report goes out with a failure line. The mail was in no report that went out.
    clock.at = T0 + 2 * DAY
    assert daily(ctx, T0 + DAY + HOUR, T0 + 2 * DAY) == listed
    clock.at = T0 + 3 * DAY
    assert daily(ctx, T0 + 2 * DAY, T0 + 3 * DAY) == t("daily_nothing", "en")


def test_carried_mail_survives_a_rerun_that_cannot_list_its_mailbox(ctx, clock, fake_himalaya,
                                                                    fake_claude):
    fake = fake_himalaya(server(qq=[message(1, "Parcel shipped")]))
    fake_claude.by_subject(SORTED)
    times = iter([0.0])
    daily(ctx, monotonic=lambda: next(times, report.DAILY_BUDGET + 1))  # the mail is left
    listed = "Orders and deliveries:\n- [qq] Shop: Your parcel is on its way."
    clock.at = T0 + DAY
    assert daily(ctx, T0, T0 + DAY) == listed  # this report is not sent

    data = fake.server()
    data["fail"] = {"qq": "connection refused"}
    fake.write(data)
    clock.at = T0 + DAY + HOUR
    assert "qq could not be read" in daily(ctx, T0, T0 + DAY + HOUR)  # this one goes out

    del data["fail"]
    fake.write(data)
    clock.at = T0 + 2 * DAY
    assert daily(ctx, T0 + DAY + HOUR, T0 + 2 * DAY) == listed
    clock.at = T0 + 3 * DAY
    assert daily(ctx, T0 + 2 * DAY, T0 + 3 * DAY) == t("daily_nothing", "en")


def reader_calls_with(fake_claude, subject: str) -> list[int]:
    """For each reader call that got the mail `subject`: how many mails it got."""
    found = []
    for call in fake_claude.calls():
        subjects = re.findall(r"^Subject: (.*)$", call["stdin"], re.MULTILINE)
        if subject in subjects:
            found.append(len(subjects))
    return found


def test_a_batch_whose_fetch_times_out_is_fetched_mail_by_mail_next_time(
        ctx, clock, fake_himalaya, fake_claude):
    fake_himalaya(server(qq=[
        message(1, "Parcel shipped"), message(2, "Friday"), message(3, "Domain renewal")]))
    fake_claude.by_subject(SORTED)
    hima = SlowFetch(ctx, {3})

    # The three are one batch, whose fetch takes too long: no mailbox failure, but the mail waits.
    assert daily(ctx, hima=hima) == "Mail not sorted in time: 3. The next report lists it."

    clock.at = T0 + DAY
    assert daily(ctx, T0, T0 + DAY, hima=hima) == (
        "Waiting for your reply:\n"
        "- [qq] Shop: Alice asks whether Friday works. · 2026-10-23\n"
        "\n"
        "Orders and deliveries:\n"
        "- [qq] Shop: Your parcel is on its way.\n"
        "\n"
        "Mail not sorted in time: 1. The next report lists it.")
    assert reader_calls_with(fake_claude, "Parcel shipped") == [1]
    assert reader_calls_with(fake_claude, "Friday") == [1]


def test_a_mail_that_times_out_alone_three_times_is_given_up(ctx, clock, fake_himalaya,
                                                             fake_claude):
    fake_himalaya(server(qq=[message(1, "Domain renewal")]))
    hima = SlowFetch(ctx, {1})
    waiting = "Mail not sorted in time: 1. The next report lists it."
    given_up = (
        "Mail in qq that took too long to fetch, three reports in a row: 1. It is left out "
        "from now on: look for it in the mailbox itself.")

    assert daily(ctx, hima=hima) == waiting
    clock.at = T0 + DAY
    assert daily(ctx, T0, T0 + DAY, hima=hima) == waiting
    clock.at = T0 + 2 * DAY
    assert daily(ctx, T0 + DAY, T0 + 2 * DAY, hima=hima) == given_up

    # That report was not sent: the one over the same window says it again, without fetching.
    hima.slow = set()
    clock.at = T0 + 2 * DAY + HOUR
    assert daily(ctx, T0 + DAY, T0 + 2 * DAY + HOUR, hima=hima) == given_up
    assert fake_claude.calls() == []
    clock.at = T0 + 3 * DAY
    assert daily(ctx, T0 + 2 * DAY + HOUR, T0 + 3 * DAY, hima=hima) == t("daily_nothing", "en")
    assert json.loads((ctx.state / "report.json").read_text("utf-8"))["left"] == []


def test_a_given_up_mail_is_not_fetched_again_while_its_window_is_covered_again(
        ctx, clock, fake_himalaya, fake_claude):
    fake_himalaya(server(qq=[message(1, "Domain renewal")]))
    fake_claude.by_subject(SORTED)
    hima = SlowFetch(ctx, {1})
    given_up = (
        "Mail in qq that took too long to fetch, three reports in a row: 1. It is left out "
        "from now on: look for it in the mailbox itself.")
    # No report goes out, so every run covers the window from SINCE again, where the mail
    # arrived.
    for hours in (0, 1):
        clock.at = T0 + hours * HOUR
        assert daily(ctx, SINCE, clock.at, hima=hima) == (
            "Mail not sorted in time: 1. The next report lists it.")
    clock.at = T0 + 2 * HOUR
    assert daily(ctx, SINCE, clock.at, hima=hima) == given_up

    hima.slow, hima.fetched = set(), []  # a fetch now would work, and show
    clock.at = T0 + 3 * HOUR
    assert daily(ctx, SINCE, clock.at, hima=hima) == given_up
    assert hima.fetched == []
    assert fake_claude.calls() == []


def test_a_server_that_cannot_be_reached_in_time_is_no_slow_mail(ctx, clock, fake_himalaya,
                                                                 fake_claude):
    late = message(1, "Parcel shipped")
    late["fail_fetch"] = "imap: connection timed out (os error 10060)"
    fake = fake_himalaya(server(qq=[late]))
    fake_claude.by_subject(SORTED)

    # The connection timed out, not the fetch: the mailbox could not be read, and its mail waits
    # to be fetched with the others, not on its own.
    assert daily(ctx) == (
        "No new mail needs your attention in the mailboxes that could be read.\n\n"
        "qq could not be read (imap: connection timed out (os error 10060)); its mail is listed "
        "once it can be read again.")
    [entry] = json.loads((ctx.state / "report.json").read_text("utf-8"))["left"]
    assert (entry["key"], entry["alone"], entry["timeouts"]) == ("qq/7/1", False, 0)

    data = fake.server()
    del data["qq"]["INBOX"]["messages"][0]["fail_fetch"]
    fake.write(data)
    clock.at = T0 + DAY
    assert daily(ctx, T0, T0 + DAY) == (
        "Orders and deliveries:\n- [qq] Shop: Your parcel is on its way.")


def test_the_arrival_listing_asks_for_one_range(ctx, fake_himalaya, fake_claude):
    # Thousands of sparse UIDs since the start's day, all of them before the window.
    fake = fake_himalaya(server(qq=[
        message(uid, "Weekly news", SINCE - HOUR) for uid in range(1, 10_000, 2)]))

    assert daily(ctx) == t("daily_nothing", "en")
    [fetch] = [
        call["argv"] for call in fake.calls()
        if "qq" in call["argv"] and "fetch" in call["argv"] and "--internal-date" in call["argv"]]
    assert "1:9999" in fetch
    assert max(len(arg) for arg in fetch[1:]) < 100  # argv[0] is the fake's own path


def test_daily_shares_the_budget_and_ends_in_time(ctx, fake_himalaya, fake_claude, monkeypatch):
    fake_himalaya(server(qq=[message(1, "Parcel shipped")], gmail=[message(2, "Friday")]))
    fake_claude.by_subject(SORTED)
    budgets = []
    real = report.triage

    def recording(*args, budget, **kwargs):
        budgets.append(budget)
        return real(*args, budget=budget, **kwargs)

    monkeypatch.setattr(report, "triage", recording)
    daily(ctx, monotonic=lambda: 0.0)

    # Each mailbox may start batches for its share of what is left of the budget.
    assert budgets == [report.DAILY_BUDGET / 2, report.DAILY_BUDGET]
    # No batch starts after 150 s, and the last one ends well before cc-hub's 300 s.
    assert report.DAILY_BUDGET <= 150
    assert report.DAILY_BUDGET + report.HIMALAYA_TIMEOUT + report.READER_TIMEOUT <= 290


# --- the command -------------------------------------------------------------------------------


def test_report_reads_cc_hub_env(ctx, monkeypatch, capsys):
    seen = {}

    def fake_daily(ctx, hima, reader, since, until, language):
        seen.update(hima=hima.timeout, reader=reader.timeout, model=reader.model, since=since,
                    until=until, language=language)
        return "the daily section"

    def fake_monthly(ctx, since, until, language):
        seen.update(monthly=(since, until, language))
        return "the monthly section"

    monkeypatch.setattr(report, "daily", fake_daily)
    monkeypatch.setattr(report, "monthly", fake_monthly)
    monkeypatch.setenv("CC_HUB_REPORT_SINCE", "2026-10-19T20:00:00+08:00")
    monkeypatch.setenv("CC_HUB_REPORT_UNTIL", "2026-10-20T20:00:00+08:00")
    monkeypatch.setenv("CC_HUB_LANGUAGE", "zh-CN")

    assert main(["report", "daily"]) == 0
    assert capsys.readouterr().out == "the daily section\n"
    assert seen == {
        "hima": report.HIMALAYA_TIMEOUT, "reader": 120.0, "model": "sonnet",
        "since": SINCE, "until": T0, "language": "zh-CN"}

    monkeypatch.delenv("CC_HUB_LANGUAGE")
    assert main(["report", "monthly"]) == 0
    assert capsys.readouterr().out == "the monthly section\n"
    assert seen["monthly"] == (SINCE, T0, "en")

    monkeypatch.delenv("CC_HUB_REPORT_SINCE")
    assert main(["report", "daily"]) == 1
    assert capsys.readouterr().err == (
        "mx: run by cc-hub's report: CC_HUB_REPORT_SINCE is not set\n")
    monkeypatch.setenv("CC_HUB_REPORT_SINCE", "yesterday")
    assert main(["report", "daily"]) == 1
    assert "CC_HUB_REPORT_SINCE" in capsys.readouterr().err


def test_report_files_name_the_command():
    for period in ("daily", "monthly"):
        text = (ROOT / "mail" / "reports" / f"{period}.md").read_text("utf-8")
        assert text == (
            "---\n"
            f"command: uv run apps/mail/scripts/mx.py report {period}\n"
            "title: Mail\n"
            "---\n"
            f"The mail app's {period} section. The script writes it; it reaches the user without "
            "passing through the agent.\n")


# --- the monthly section -----------------------------------------------------------------------


def pinned(name: str) -> str:
    return TOOLS[name].version


def test_monthly_versions(ctx):
    def latest(repo: str) -> str | None:
        return "v2.3.0" if repo == "himalaya" else pinned(repo)

    assert report.monthly(ctx, T0 - 30 * DAY, T0, "en", latest=latest) == (
        "himalaya v2.3.0 is out; the mail app still uses v2.2.1.")
    assert report.monthly(ctx, T0 - 30 * DAY, T0, "zh", latest=latest) == (
        "himalaya 出了 v2.3.0，邮件应用用的还是 v2.2.1。")
    assert report.monthly(ctx, T0 - 30 * DAY, T0, "en", latest=pinned) == (
        "himalaya, carillon and ortie are at their latest releases.")


def test_monthly_cannot_check(ctx):
    assert report.monthly(ctx, T0 - 30 * DAY, T0, "en", latest=lambda repo: None) == (
        "The latest releases of some tools could not be checked: GitHub did not answer.")

    def latest(repo: str) -> str | None:
        return {"himalaya": "v2.3.0", "carillon": None}.get(repo, pinned(repo))

    assert report.monthly(ctx, T0 - 30 * DAY, T0, "en", latest=latest) == (
        "himalaya v2.3.0 is out; the mail app still uses v2.2.1.\n"
        "\n"
        "The latest releases of some tools could not be checked: GitHub did not answer.")


def test_monthly_alerts_and_mailbox_problems(hub, clock):
    alerts_on(hub)
    write_mailboxes(hub, BOXES)
    ctx = load(hub.root)
    beat(ctx)
    health = Health(ctx)
    health.carillon_down(T0 - 3 * DAY)
    health.carillon_up(T0 - 2 * DAY + 3 * HOUR)
    for at in (T0 - 10 * DAY, T0 - 5 * DAY):
        health.mailbox_failed("qq", "auth", "refused", at)
        health.mailbox_ok("qq", at + HOUR)
    health.mailbox_failed("gmail", "connect", "timed out", T0 - DAY)
    health.mailbox_failed("qq", "auth", "refused", T0 - 40 * DAY)  # before the month
    health.mailbox_ok("qq", T0 - 39 * DAY)

    text = report.monthly(ctx, T0 - 30 * DAY, T0, "en", latest=pinned)
    assert text == (
        "himalaya, carillon and ortie are at their latest releases.\n"
        "\n"
        "Alerts were down for 1 d 3 h this month.\n"
        "\n"
        "qq: the server refused the login, 2 times this month.\n"
        "\n"
        "gmail: it could not be checked, once this month.")
    assert report.monthly(ctx, T0 - 30 * DAY, T0, "zh", latest=pinned).splitlines()[2:] == [
        "这个月实时提醒有 1 天 3 小时没有运行。", "",
        "qq：服务器拒绝了登录，本月 2 次。", "",
        "gmail：无法检查，本月 1 次。"]

    clock.at = T0 + 31 * DAY
    beat(ctx, clock.at)
    assert report.monthly(ctx, T0, T0 + 31 * DAY, "en", latest=pinned) == (
        "himalaya, carillon and ortie are at their latest releases.\n"
        "\n"
        "Alerts ran the whole month.\n"
        "\n"
        "gmail: it could not be checked, once this month.")


def test_latest_release_reads_the_tag_and_fails_soft():
    asked = []

    def fetch(url: str, timeout: float) -> bytes:
        asked.append((url, timeout))
        return json.dumps({"tag_name": "v2.3.0", "name": "whatever"}).encode()

    assert report.latest_release("himalaya", fetch=fetch) == "v2.3.0"
    assert asked == [("https://api.github.com/repos/pimalaya/himalaya/releases/latest", 10)]

    def failing(url: str, timeout: float) -> bytes:
        raise OSError("timed out")

    for answer in (failing, lambda url, timeout: b"not json",
                   lambda url, timeout: b'{"tag_name": 3}',
                   lambda url, timeout: b'{"tag_name": "v1\\nInjected line"}',
                   lambda url, timeout: b"[]"):
        assert report.latest_release("himalaya", fetch=answer) is None


# --- the texts ---------------------------------------------------------------------------------


def test_every_string_key_in_both_languages():
    assert STRINGS["en"].keys() == STRINGS["zh"].keys()
    for key, text in STRINGS["en"].items():  # the same values to fill in, in both languages
        assert set(re.findall(r"{(\w+)}", text)) == set(re.findall(r"{(\w+)}", STRINGS["zh"][key]))


def test_chinese_texts_use_cc_hubs_names_for_the_reports():
    for text in STRINGS["zh"].values():
        assert "汇报" not in text
    assert "日报" in STRINGS["zh"]["reader_unsorted"]

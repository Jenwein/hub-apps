"""Rules, catch-up marks, stored verdicts and triage (mailkit.rules, marks, verdicts, triage).

The server is the fake himalaya and the reader the fake claude (fakes.py); a few tests hand
`triage` a reader object of their own, to time it or to script its failures exactly.
"""

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
from fakes import envelope
from mailkit import clock
from mailkit.cli import main
from mailkit.context import load
from mailkit.himalaya import AuthError, Himalaya
from mailkit.mailtext import parse
from mailkit.marks import (
    MAX_CATCH_UP,
    Mark,
    fresh,
    load_marks,
    save_marks,
    uid_set,
    verdict_key,
)
from mailkit.reader import Reader, ReaderDown, ReaderError, Verdict
from mailkit.rules import combined, default_rules, user_rules
from mailkit.triage import BATCH, FALLBACK_REASON, WORKERS, triage
from mailkit.verdicts import KEEP, VerdictStore
from mailtest import write_mailboxes

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
MARKER = re.compile(r"<<<MAIL-([0-9a-f]{16}) (\d+)>>>\n(.*?)\n<<<END MAIL-\1 \2>>>", re.DOTALL)
URGENT = {
    "summary": "A card payment failed.", "code": "", "amount": "", "deadline": "",
    "tier": "now", "category": "money", "reason": "A payment failed.", "phishing": False}


@pytest.fixture
def ctx(hub, monkeypatch):
    """The hub with the mailboxes qq and gmail, at T0."""
    write_mailboxes(hub, BOXES)
    monkeypatch.setattr(clock, "now", lambda: T0)
    return load(hub.root)


def eml(subject: str, when: datetime, sender: str = "Shop <shop@example.org>") -> str:
    return (
        f"From: {sender}\nTo: me@qq.com\nSubject: {subject}\nDate: {format_datetime(when)}\n"
        f"Content-Type: text/plain; charset=utf-8\n\nThe text of {subject}.\n")


def message(uid: int, when: datetime, subject: str | None = None) -> dict:
    """A mail that arrived at `when` (its INTERNALDATE) and is dated then."""
    return {
        "uid": uid, "date": when.isoformat(), "internal_date": when.isoformat(), "seen": False,
        "raw": eml(subject or f"mail {uid}", when)}


def inbox(messages: list[dict], uidvalidity: str = "7", uidnext: int | None = None) -> dict:
    folder = {"uidvalidity": uidvalidity, "messages": messages}
    if uidnext is not None:
        folder["uidnext"] = uidnext
    return folder


def server(qq: dict, gmail: dict | None = None, **extra) -> dict:
    return {"qq": {"INBOX": qq}, "gmail": {"INBOX": gmail or inbox([], "3")}, **extra}


def numbered(uids, start: datetime = T0 - timedelta(days=1)) -> list[dict]:
    """Mails with these UIDs, a minute apart from `start`."""
    return [message(uid, start + timedelta(minutes=uid)) for uid in uids]


def words(call: dict) -> list[str]:
    """The command words of a logged himalaya call: argv after `-c himalaya.toml -a <mailbox>
    [--json]`."""
    rest = call["argv"][5:]
    return rest[1:] if rest[:1] == ["--json"] else rest


def items(uids, mailbox: str = "qq", validity: str = "7") -> list[tuple[str, str, str, int]]:
    return [(verdict_key(mailbox, validity, str(u)), mailbox, "INBOX", u) for u in uids]


def keys_of(rows) -> list[str]:
    return [row[0] for row in rows]


def mails_in(call: dict) -> list[str]:
    """The texts of the mails a reader call got on stdin."""
    return [text for _, _, text in MARKER.findall(call["stdin"])]


def subject_of(text: str) -> str:
    return re.search(r"^Subject: (.*)$", text, re.MULTILINE).group(1)


def verdict(key: str, when: datetime | None = T0, **fields) -> Verdict:
    mailbox = key.split("/")[0]
    return Verdict(**{
        "key": key, "mailbox": mailbox, "sender": "Shop", "subject": "s",
        "date": when.isoformat() if when else "", "summary": "x", "code": None, "amount": None,
        "deadline": None, "tier": "daily", "category": "other", "reason": "r", "phishing": False,
        **fields})


# --- rules -------------------------------------------------------------------------------------


def test_user_rules_come_after_the_defaults_and_win(hub, capsys):
    (hub.data / "rules.md").write_text("GitHub notifications: ignore\n", "utf-8")
    ctx = load(hub.root)
    default = (ctx.app / "rules" / "default.md").read_text("utf-8").strip()

    assert default_rules(ctx) == default
    assert user_rules(ctx) == "GitHub notifications: ignore"
    assert combined(ctx) == (
        f"Default rules:\n\n{default}\n\nThe user's own rules. Where they disagree with the "
        "default rules, the user's rules win:\n\nGitHub notifications: ignore")

    assert main(["--hub", str(hub.root), "rules"]) == 0
    out = capsys.readouterr()
    assert out.out == combined(ctx) + "\n" and out.err == ""  # plain text, not JSON


def test_without_a_rules_file_the_user_has_no_rules(hub):
    (hub.data / "rules.md").unlink()
    ctx = load(hub.root)
    assert user_rules(ctx) == ""
    assert combined(ctx).endswith("the user's rules win:\n\n")


def test_user_rules_written_with_a_byte_order_mark_read_clean(hub):
    (hub.data / "rules.md").write_bytes("﻿GitHub 的通知：忽略\n".encode())
    assert user_rules(load(hub.root)) == "GitHub 的通知：忽略"


def test_default_rules_have_the_three_tiers(hub):
    text = default_rules(load(hub.root))
    assert text.isascii()  # everything the agent and the reader read is English
    assert re.findall(r"^## (\w+)", text, re.MULTILINE) == ["now", "daily", "ignore"]
    now, daily, ignore = re.split(r"^## \w+.*$", text, flags=re.MULTILINE)[1:]
    assert "declined" in now and "sign-in" in now and "two-step" in now
    assert "waiting for an answer" in daily and "receipts" in daily and "delivery" in daily
    assert "newsletters" in ignore.lower() and "social network" in ignore.lower()
    assert text.splitlines()[-1] == "When unsure between two tiers, take the more urgent one."


def test_look_sorts_by_the_combined_rules(hub, fake_himalaya, fake_claude, capsys):
    write_mailboxes(hub, BOXES)
    (hub.data / "rules.md").write_text("Mail from my landlord: daily report\n", "utf-8")
    fake_himalaya(server(inbox(numbered([1]))))
    assert main(["--hub", str(hub.root), "list"]) == 0
    capsys.readouterr()

    assert main(["--hub", str(hub.root), "look", "1"]) == 0

    [call] = fake_claude.calls()
    prompt = call["argv"][call["argv"].index("--system-prompt") + 1]
    assert prompt.endswith(combined(load(hub.root)))
    assert prompt.endswith("the user's rules win:\n\nMail from my landlord: daily report")


# --- marks -------------------------------------------------------------------------------------


def test_verdict_key_holds_the_uidvalidity():
    assert verdict_key("qq", "7", "12") == "qq/7/12"
    assert verdict_key("qq", "8", "12") != verdict_key("qq", "7", "12")


def test_uid_set_joins_runs():
    assert uid_set([1, 2, 3, 5, 7, 8]) == "1:3,5,7:8"
    assert uid_set([9, 4, 4, 5]) == "4:5,9"
    assert uid_set([6]) == "6"


def test_first_run_sets_baseline_without_new_mail(ctx, fake_himalaya):
    fake = fake_himalaya(server(inbox(numbered(range(1, 31)))))

    uids, mark, skipped = fresh(Himalaya(ctx), ctx.mailbox("qq"), None, T0)

    assert (uids, skipped) == ([], 0)  # a full inbox is old news: no alert for any of it
    assert mark == Mark("7", 30, T0.isoformat())
    assert [words(c) for c in fake.calls()] == [["imap", "status", "INBOX"]]


def test_quiet_sweep_is_one_call(ctx, fake_himalaya):
    fake = fake_himalaya(server(inbox(numbered(range(1, 31)))))
    mark = Mark("7", 30, T0.isoformat())

    assert fresh(Himalaya(ctx), ctx.mailbox("qq"), mark, T0) == ([], mark, 0)
    assert [words(c) for c in fake.calls()] == [["imap", "status", "INBOX"]]


def test_uid_gap_returns_every_new_mail(ctx, fake_himalaya):
    late = T0 + timedelta(minutes=5)
    fake = fake_himalaya(server(inbox([*numbered(range(1, 7)), message(9, late)])))
    old = Mark("7", 5, T0.isoformat())

    uids, mark, skipped = fresh(Himalaya(ctx), ctx.mailbox("qq"), old, T0)

    assert (uids, skipped) == ([6, 9], 0)
    assert mark == Mark("7", 9, late.isoformat())  # the highest UID handled and its arrival
    assert [words(c) for c in fake.calls()] == [
        ["imap", "status", "INBOX"], ["imap", "fetch", "-m", "INBOX", "6:*", "--internal-date"]]


def test_nothing_newer_ignores_the_highest_uid_quirk(ctx, fake_himalaya):
    # Mail 6 came and went: UIDNEXT is 7, and "6:*" answers the highest mail there is, 5.
    fake = fake_himalaya(server(inbox(numbered(range(1, 6)), uidnext=7)))
    old = Mark("7", 5, T0.isoformat())

    uids, mark, skipped = fresh(Himalaya(ctx), ctx.mailbox("qq"), old, T0)

    assert (uids, skipped) == ([], 0)
    # No UID up to 6 is left to come, so the mark moves past the gap: the next quiet sweep is
    # one call again.
    assert mark == Mark("7", 6, old.last_date)
    assert fresh(Himalaya(ctx), ctx.mailbox("qq"), mark, T0) == ([], mark, 0)
    assert [words(c)[:2] for c in fake.calls()] == [
        ["imap", "status"], ["imap", "fetch"], ["imap", "status"]]


def test_uidvalidity_change_falls_back_to_date(ctx, fake_himalaya):
    # The server renumbered the inbox (UIDVALIDITY 7 -> 8). The mark's time is 08:00 at +08:00,
    # the first second of 20 October UTC.
    last = datetime(2026, 10, 20, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    old_mail = [message(uid, last - timedelta(days=3, minutes=uid)) for uid in range(1, 41)]
    just_before = message(41, datetime(2026, 10, 19, 23, 30, tzinfo=UTC))
    same_second = message(42, datetime(2026, 10, 20, 0, 0, tzinfo=UTC))
    after = message(43, datetime(2026, 10, 20, 0, 0, 1, tzinfo=UTC))
    # Arrived after the mark, and the server, at -05:00, dates it the day before.
    west = datetime(2026, 10, 19, 20, 0, tzinfo=timezone(timedelta(hours=-5)))
    fake = fake_himalaya(server(inbox(
        [*old_mail, just_before, same_second, after, message(44, west)], uidvalidity="8")))
    old = Mark("7", 900, last.isoformat())

    uids, mark, skipped = fresh(Himalaya(ctx), ctx.mailbox("qq"), old, T0)

    # Only the mail that arrived after the mark: not one alert per renumbered mail.
    assert (uids, skipped) == ([43, 44], 0)
    assert mark == Mark("8", 44, west.isoformat())
    assert [words(c) for c in fake.calls()] == [
        ["imap", "status", "INBOX"],
        # The day of the mark where the day starts last (-12:00): the server's SINCE takes the
        # date in its own time zone.
        ["imap", "search", "-m", "INBOX", "--since", "2026-10-19"],
        ["imap", "fetch", "-m", "INBOX", "41:44", "--internal-date"]]


def test_uidvalidity_change_with_nothing_newer_takes_the_new_numbers(ctx, fake_himalaya):
    fake_himalaya(server(inbox(numbered(range(1, 11)), uidvalidity="8")))
    old = Mark("7", 900, T0.isoformat())

    uids, mark, skipped = fresh(Himalaya(ctx), ctx.mailbox("qq"), old, T0)

    assert (uids, skipped) == ([], 0)
    assert mark == Mark("8", 10, T0.isoformat())


def test_catch_up_is_capped(ctx, fake_himalaya):
    fake_himalaya(server(inbox(numbered(range(1, 256)))))
    old = Mark("7", 5, T0.isoformat())

    uids, mark, skipped = fresh(Himalaya(ctx), ctx.mailbox("qq"), old, T0)

    assert MAX_CATCH_UP == 200
    assert uids == list(range(56, 256)) and skipped == 50  # the newest 200, oldest first
    assert mark.last_uid == 255


def test_marks_are_saved_per_mailbox(ctx):
    qq, gmail = Mark("7", 5, T0.isoformat()), Mark("3", 9, T0.isoformat())
    assert load_marks(ctx) == {}

    save_marks(ctx, {"qq": qq})
    save_marks(ctx, {"gmail": gmail})  # a mailbox saved alone keeps the others

    assert load_marks(ctx) == {"qq": qq, "gmail": gmail}
    data = json.loads((ctx.state / "marks.json").read_text("utf-8"))
    assert data["qq"] == {"uidvalidity": "7", "last_uid": 5, "last_date": T0.isoformat()}


def test_a_bad_mark_is_no_mark(ctx):
    (ctx.state / "marks.json").write_text(json.dumps({
        "qq": {"uidvalidity": "7", "last_uid": "5", "last_date": T0.isoformat()},
        "gmail": {"uidvalidity": "3", "last_uid": 9, "last_date": "yesterday"},
        "other": [1, 2],
        "fine": {"uidvalidity": "1", "last_uid": 2, "last_date": T0.isoformat()}}), "utf-8")

    # A mailbox without a usable mark starts again from a baseline: never a storm.
    assert load_marks(ctx) == {"fine": Mark("1", 2, T0.isoformat())}


# --- the verdict store -------------------------------------------------------------------------


def test_store_keeps_verdicts_and_alerts(ctx):
    store = VerdictStore(ctx)
    first = verdict("qq/7/1", tier="now")
    assert store.get(first.key) is None and not store.alerted(first.key)

    store.put(first)
    assert store.get(first.key) == first
    assert not store.alerted(first.key)

    store.mark_alerted(first.key, T0)
    assert store.alerted(first.key)
    store.put(replace(first, summary="again"))  # sorted again: the alert stays on record
    assert store.alerted(first.key) and store.get(first.key).summary == "again"

    store.mark_alerted("qq/7/2", T0)  # an alert of a mail with no verdict is recorded too
    assert store.alerted("qq/7/2") and store.get("qq/7/2") is None


def test_store_between_goes_by_the_mail_date(ctx):
    store = VerdictStore(ctx)
    early = verdict("qq/7/1", T0 - timedelta(hours=2))
    start = verdict("qq/7/2", T0 - timedelta(hours=1))
    late = verdict("gmail/3/1", (T0 - timedelta(minutes=30)).astimezone(
        timezone(timedelta(hours=8))))
    end = verdict("qq/7/3", T0)
    no_date = verdict("qq/7/4", None)
    store.put_many([end, late, early, start, no_date])

    assert store.between(T0 - timedelta(hours=1), T0) == [start, late]


def test_old_verdicts_are_dropped(ctx, monkeypatch):
    store = VerdictStore(ctx)
    old = verdict("qq/7/1", T0 - KEEP - timedelta(days=1))
    recent = verdict("qq/7/2", T0 - KEEP + timedelta(days=1))
    alerted = verdict("qq/7/3", T0 - timedelta(days=400), tier="now")  # a back-dated Date
    undated = verdict("qq/7/4", None)
    future = verdict("qq/7/5", T0 + timedelta(days=4000))  # a Date far ahead

    store.put_many([old, recent, alerted, undated, future])
    store.mark_alerted(alerted.key, T0)

    assert KEEP == timedelta(days=62)
    assert store.get(old.key) is None  # dropped as soon as it is written
    assert store.get(recent.key) == recent and store.get(undated.key) == undated
    assert store.get(future.key) == future
    assert store.alerted(alerted.key)  # its alert is recent: it is never alerted twice

    later = T0 + timedelta(days=3)
    monkeypatch.setattr(clock, "now", lambda: later)
    store.put(verdict("qq/7/9", later))
    assert store.get(recent.key) is None
    assert store.get(undated.key) == undated  # dated by when it was stored

    much_later = T0 + KEEP + timedelta(days=1)
    monkeypatch.setattr(clock, "now", lambda: much_later)
    store.put(verdict("qq/7/10", much_later))
    data = json.loads((ctx.state / "verdicts.json").read_text("utf-8"))
    assert set(data) == {"qq/7/9", "qq/7/10"}  # the alert, the undated and the future mail too


def test_the_store_takes_writes_from_many_threads(ctx):
    store = VerdictStore(ctx)
    many = [verdict(f"qq/7/{n}") for n in range(12)]

    with ThreadPoolExecutor(6) as pool:
        list(pool.map(store.put, many))

    assert store.get_many([v.key for v in many]) == {v.key: v for v in many}


# --- triage ------------------------------------------------------------------------------------


def test_triage_reuses_stored_verdicts(ctx, fake_himalaya, fake_claude):
    fake = fake_himalaya(server(inbox(numbered([1, 2, 3]))))
    rows = items([1, 2, 3])
    stored = verdict(rows[1][0], tier="now", summary="Sorted before.")
    VerdictStore(ctx).put(stored)

    result = triage(ctx, Himalaya(ctx), Reader("sonnet"), rows, budget=60)

    assert [v.key for v in result.verdicts] == keys_of(rows) and result.left == []
    assert result.verdicts[1] == stored
    [call] = fake_claude.calls()
    assert [subject_of(text) for text in mails_in(call)] == ["mail 1", "mail 3"]
    fetches = [words(c) for c in fake.calls()]
    assert fetches == [["imap", "fetch", "-m", "INBOX", "1,3", "--body"]]


def test_triage_runs_batches_of_10(ctx, fake_himalaya, fake_claude):
    fake = fake_himalaya(server(inbox(numbered(range(1, 26)))))
    rows = items(range(1, 26))

    result = triage(ctx, Himalaya(ctx), Reader("sonnet"), rows, budget=600)

    assert BATCH == 10 and WORKERS == 3
    assert sorted(len(mails_in(c)) for c in fake_claude.calls()) == [5, 10, 10]
    fetches = sorted(words(c) for c in fake.calls())
    assert fetches == sorted(
        ["imap", "fetch", "-m", "INBOX", ",".join(str(u) for u in uids), "--body"]
        for uids in (range(1, 11), range(11, 21), range(21, 26)))
    assert [v.key for v in result.verdicts] == keys_of(rows) and result.left == []
    assert all(v.summary == "A mail." for v in result.verdicts)  # the fake reader's answer
    stored = VerdictStore(ctx).get_many(keys_of(rows))
    assert stored == {v.key: v for v in result.verdicts}
    # The reader got the rules and the language; reading marked nothing as read.
    for call in fake_claude.calls():
        assert call["argv"][call["argv"].index("--system-prompt") + 1].endswith(combined(ctx))
    assert not any("--seen" in c["argv"] for c in fake.calls())
    assert not any(m["seen"] for m in fake.server()["qq"]["INBOX"]["messages"])


def test_triage_never_mixes_mailboxes_in_a_batch(ctx, fake_himalaya, fake_claude):
    fake = fake_himalaya(server(inbox(numbered([1, 2, 3])), inbox(numbered([1, 2]), "3")))
    rows = items([1, 2, 3]) + items([1, 2], "gmail", "3")

    result = triage(ctx, Himalaya(ctx), Reader("sonnet"), rows, budget=60)

    assert sorted(len(mails_in(c)) for c in fake_claude.calls()) == [2, 3]
    assert sorted(c["argv"][4] for c in fake.calls()) == ["gmail", "qq"]
    assert [(v.key, v.mailbox) for v in result.verdicts] == [(k, m) for k, m, _, _ in rows]


def test_a_mail_the_server_no_longer_has_is_neither_sorted_nor_left(ctx, fake_himalaya,
                                                                     fake_claude):
    fake_himalaya(server(inbox(numbered([1, 2]))))
    rows = items([1, 2, 3])

    result = triage(ctx, Himalaya(ctx), Reader("sonnet"), rows, budget=60)

    assert [v.key for v in result.verdicts] == keys_of(rows[:2]) and result.left == []


class SlowReader:
    """A reader that takes `seconds` per call and counts how many calls run at once."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.lock = threading.Lock()
        self.running = self.most = self.calls = 0

    def triage(self, mails, rules, language):
        with self.lock:
            self.calls += 1
            self.running += 1
            self.most = max(self.most, self.running)
        time.sleep(self.seconds)
        with self.lock:
            self.running -= 1
        return [verdict(key) for key, _, _ in mails]


def test_budget_leaves_the_rest(ctx, fake_himalaya):
    fake = fake_himalaya(server(inbox(numbered(range(1, 51)))))
    rows = items(range(1, 51))
    reader = SlowReader(2.0)

    result = triage(ctx, Himalaya(ctx), reader, rows, budget=1.0)

    # Three batches start at once; the other two would start after the budget: left.
    assert (reader.calls, reader.most) == (WORKERS, WORKERS)
    assert [v.key for v in result.verdicts] == keys_of(rows[:30])
    assert result.left == keys_of(rows[30:])
    assert len([c for c in fake.calls() if words(c)[:2] == ["imap", "fetch"]]) == 3
    assert set(VerdictStore(ctx).get_many(keys_of(rows))) == set(keys_of(rows[:30]))


def test_a_fetch_that_fails_raises_after_the_other_batches_are_stored(ctx, fake_himalaya,
                                                                      fake_claude):
    fake_himalaya(server(
        inbox(numbered([1, 2])), inbox(numbered([1]), "3"),
        fail={"gmail": "imap: authentication failed"}))
    rows = items([1, 2]) + items([1], "gmail", "3")

    with pytest.raises(AuthError):
        triage(ctx, Himalaya(ctx), Reader("sonnet"), rows, budget=60)

    assert set(VerdictStore(ctx).get_many(keys_of(rows))) == set(keys_of(rows[:2]))


# --- a batch the reader cannot sort (R23) ------------------------------------------------------


def test_one_bad_mail_gets_a_fallback_and_the_rest_real_verdicts(ctx, fake_himalaya, fake_claude):
    poison = message(4, T0 - timedelta(minutes=50), "poison pill")
    fake_himalaya(server(inbox([*numbered([1, 2, 3]), poison, *numbered(range(5, 11))])))
    fake_claude.by_subject({"poison": {"tier": "bogus"}, "": URGENT})
    rows = items(range(1, 11))

    result = triage(ctx, Himalaya(ctx), Reader("sonnet"), rows, budget=600)

    assert result.left == []
    assert [v.key for v in result.verdicts] == keys_of(rows)
    fallback = result.verdicts[3]
    assert fallback == Verdict(
        key=rows[3][0], mailbox="qq", sender="Shop", subject="poison pill",
        date=(T0 - timedelta(minutes=50)).isoformat(), summary="", code=None, amount=None,
        deadline=None, tier="daily", category="other", reason=FALLBACK_REASON, phishing=False)
    assert FALLBACK_REASON == "could not be sorted"
    others = result.verdicts[:3] + result.verdicts[4:]
    assert all((v.tier, v.summary) == ("now", URGENT["summary"]) for v in others)
    # The batch, then each mail alone.
    assert [len(mails_in(c)) for c in fake_claude.calls()] == [10] + [1] * 10
    assert VerdictStore(ctx).get_many(keys_of(rows)) == {v.key: v for v in result.verdicts}


def test_mails_that_all_get_unfitting_answers_all_get_fallbacks(ctx, fake_himalaya, fake_claude):
    # The reader answers, but never fits: that is what the mails make it say, so a batch of
    # crafted mails cannot hold the mailbox back for good.
    fake_himalaya(server(inbox(numbered([1, 2, 3]))))
    fake_claude.by_subject({"": {"tier": "bogus"}})
    rows = items([1, 2, 3])

    result = triage(ctx, Himalaya(ctx), Reader("sonnet"), rows, budget=600)

    assert [(v.key, v.reason) for v in result.verdicts] == [
        (key, FALLBACK_REASON) for key in keys_of(rows)]
    assert [len(mails_in(c)) for c in fake_claude.calls()] == [3, 1, 1, 1]


def test_a_bad_mail_alone_gets_its_fallback_from_one_call(ctx, fake_himalaya, fake_claude):
    fake_himalaya(server(inbox([message(1, T0, "poison pill")])))
    fake_claude.by_subject({"poison": {"index": 7}})
    rows = items([1])

    result = triage(ctx, Himalaya(ctx), Reader("sonnet"), rows, budget=60)

    assert [(v.tier, v.reason) for v in result.verdicts] == [("daily", FALLBACK_REASON)]
    assert len(fake_claude.calls()) == 1
    assert VerdictStore(ctx).get(rows[0][0]) == result.verdicts[0]


@pytest.mark.parametrize("mode, exe", [("error", None), ("garbage", None), ("", "no-claude-6f0e")])
def test_a_reader_that_fails_for_every_mail_raises_and_stores_nothing(
        ctx, fake_himalaya, fake_claude, mode, exe):
    fake_himalaya(server(inbox(numbered(range(1, 26)))))
    fake_claude.mode(mode)
    rows = items(range(1, 26))

    with pytest.raises(ReaderDown):
        triage(ctx, Himalaya(ctx), Reader("sonnet", exe=exe), rows, budget=600)

    assert VerdictStore(ctx).get_many(keys_of(rows)) == {}
    # Each batch, then its first mail alone, is all it takes to know the reader is down.
    assert len(fake_claude.calls()) <= 2 * 3


def test_reader_down_is_a_reader_that_did_not_answer(fake_claude):
    mail = [("qq/7/1", "qq", parse(eml("hello", T0).encode()))]

    def failure(reader: Reader | None = None) -> ReaderError:
        with pytest.raises(ReaderError) as caught:
            (reader or Reader("sonnet")).triage(mail, "", "en")
        return caught.value

    for mode in ("error", "garbage"):
        fake_claude.mode(mode)
        assert isinstance(failure(), ReaderDown)
    fake_claude.mode("")
    assert isinstance(failure(Reader("sonnet", exe="no-claude-6f0e")), ReaderDown)
    fake_claude.reply_raw(envelope(None, result="API Error: 529 overloaded", is_error=True))
    assert isinstance(failure(), ReaderDown)
    # An answer came back, but not one that fits the mails: that is about the mails.
    fake_claude.by_subject({"": {"tier": "bogus"}})
    assert not isinstance(failure(), ReaderDown)
    fake_claude.reply_raw(envelope(None, result="I would rather not."))
    assert not isinstance(failure(), ReaderDown)
    fake_claude.reply_raw({
        "type": "result", "subtype": "error_max_structured_output_retries", "is_error": True})
    assert not isinstance(failure(), ReaderDown)


def test_a_reader_that_does_not_answer_in_time_is_down(fake_claude):
    fake_claude.mode("sleep")
    with pytest.raises(ReaderDown, match="did not answer within 1 s"):
        Reader("sonnet", timeout=1).triage(
            [("qq/7/1", "qq", parse(eml("hello", T0).encode()))], "", "en")


class ScriptedReader:
    """A reader whose calls go as `script` says, in order: an error to raise, or None for an
    answer. `then` runs before each call (the time passing)."""

    def __init__(self, script: list[Exception | None], then=lambda: None):
        self.script = list(script)
        self.then = then
        self.sizes: list[int] = []

    def triage(self, mails, rules, language):
        self.then()
        self.sizes.append(len(mails))
        outcome = self.script.pop(0)
        if outcome is not None:
            raise outcome
        return [verdict(key, summary="sorted") for key, _, _ in mails]


def test_a_batch_that_times_out_is_sorted_mail_by_mail(ctx, fake_himalaya):
    fake_himalaya(server(inbox(numbered([1, 2, 3]))))
    reader = ScriptedReader([ReaderDown("the reader did not answer within 240 s"), None, None, None])
    rows = items([1, 2, 3])

    result = triage(ctx, Himalaya(ctx), reader, rows, budget=600)

    assert reader.sizes == [3, 1, 1, 1]
    assert [(v.key, v.summary) for v in result.verdicts] == [(k, "sorted") for k in keys_of(rows)]


def test_the_reader_going_down_mid_batch_raises_and_keeps_what_was_sorted(ctx, fake_himalaya):
    fake_himalaya(server(inbox(numbered([1, 2, 3]))))
    reader = ScriptedReader([ReaderError("the reader answered for 2 of 3 mails"), None,
                             ReaderDown("Not logged in")])
    rows = items([1, 2, 3])

    with pytest.raises(ReaderDown, match="Not logged in"):
        triage(ctx, Himalaya(ctx), reader, rows, budget=600)

    assert reader.sizes == [3, 1, 1]
    assert set(VerdictStore(ctx).get_many(keys_of(rows))) == {rows[0][0]}


def test_a_batch_whose_first_mail_alone_fails_the_same_way_raises(ctx, fake_himalaya):
    fake_himalaya(server(inbox(numbered([1, 2, 3]))))
    reader = ScriptedReader([ReaderDown("Not logged in"), ReaderDown("Not logged in")])
    rows = items([1, 2, 3])

    with pytest.raises(ReaderDown):
        triage(ctx, Himalaya(ctx), reader, rows, budget=600)

    assert reader.sizes == [3, 1]
    assert VerdictStore(ctx).get_many(keys_of(rows)) == {}


@pytest.mark.parametrize("error, raised", [
    (ReaderError("the reader answered for 2 of 3 mails"), False),
    (ReaderDown("the reader did not answer within 240 s"), True)])
def test_no_mail_by_mail_retry_after_the_budget(ctx, fake_himalaya, error, raised):
    fake_himalaya(server(inbox(numbered([1, 2, 3]))))
    now = [0.0]

    def spend() -> None:
        now[0] += 100.0  # the call takes longer than the whole budget

    reader = ScriptedReader([error], then=spend)
    rows = items([1, 2, 3])

    if raised:  # the reader did not answer, and there is no time left to try it on one mail
        with pytest.raises(ReaderDown):
            triage(ctx, Himalaya(ctx), reader, rows, budget=60, monotonic=lambda: now[0])
    else:  # the answer did not fit: the mails wait for the next run
        result = triage(ctx, Himalaya(ctx), reader, rows, budget=60, monotonic=lambda: now[0])
        assert (result.verdicts, result.left) == ([], keys_of(rows))
    assert reader.sizes == [3]
    assert VerdictStore(ctx).get_many(keys_of(rows)) == {}

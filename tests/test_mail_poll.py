import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mailfakes import FakeServer, fake_connect, make_raw
from mailkit import accounts, cli, message, poll, server
from mailkit.config import read_text, set_mailbox_value_text, write_text
from mailkit.server import LoginError, ServerError
from mailkit.store import Store

SEED_MAIL = Path(__file__).parents[1] / "mail" / "seed" / "mail"
NOW = datetime(2026, 10, 14, 4, 0, tzinfo=UTC)
WORK = "me@corp.example"
QQ = "me@qq.com"
OLD = NOW - timedelta(days=200)  # before the 90 days that are backfilled
RECENT = NOW - timedelta(days=10)


def t(minutes: float = 0) -> datetime:
    return NOW + timedelta(minutes=minutes)


@pytest.fixture
def hub_dir(tmp_path):
    hub = tmp_path / "hub"
    shutil.copytree(SEED_MAIL, hub / "mail")
    (hub / "secrets").mkdir()
    return hub


def add(hub, fake, name="工作", address=WORK, provider="feishu", password="pw"):
    r = accounts.add(hub, hub / "mail", name, address, password, provider=provider,
                     connect=fake_connect(fake), now=NOW)
    assert r["ok"], r


def run(hub, servers, minutes: float = 0, **options):
    return poll.poll(hub, hub / "mail", connect=fake_connect(servers), now=t(minutes), **options)


class Waker:
    """A `wake` for poll(): counts its calls; returns `error` (None: the wake worked)."""

    def __init__(self, error=None):
        self.calls = 0
        self.error = error

    def __call__(self):
        self.calls += 1
        return self.error


def rows(hub, address=None):
    """(address, uid, uidvalidity, status, addressed, subject) of every indexed message."""
    db = sqlite3.connect(hub / "mail" / ".state" / "mail.db")
    try:
        query = "SELECT address, uid, uidvalidity, status, addressed, subject FROM messages"
        params = ()
        if address is not None:
            query += " WHERE address = ?"
            params = (address,)
        return db.execute(query + " ORDER BY uid, id", params).fetchall()
    finally:
        db.close()


def state(hub, address=WORK):
    with Store.open(hub / "mail") as store:
        return store.state(address)


def problems(hub, unreported_only=False):
    with Store.open(hub / "mail") as store:
        return store.problems(unreported_only=unreported_only)


def report_problems(hub, minutes):
    """What triage does after telling the user: mark every listed problem reported."""
    with Store.open(hub / "mail") as store:
        ids = [p["id"] for p in store.problems(unreported_only=True)]
        store.mark_reported(ids, t(minutes))


def set_mailbox(hub, name, key, value):
    mail = hub / "mail"
    write_text(mail, set_mailbox_value_text(read_text(mail), name, key, value))


def mail_from(sender, subject="hi", to=WORK, **options):
    return make_raw(subject, sender, to, f"body of {subject}", **options)


# -- new mail and waking ---------------------------------------------------------------------


def test_new_mail_after_baseline_is_pending_and_wakes_once(hub_dir):
    fake = FakeServer()
    for i in range(3):
        fake.deliver(mail_from("a@x.example", f"old {i}"), received=OLD)
    add(hub_dir, fake)
    assert state(hub_dir) == {"uidvalidity": 1, "last_uid": 3, "backfill_before": None}
    fake.deliver(mail_from("boss@corp.example", "to me"))
    fake.deliver(mail_from("boss@corp.example", "cc me", to="team@corp.example", cc=WORK))
    waker = Waker()

    r = run(hub_dir, fake, wake=waker)
    assert r == {"mailboxes": [{"name": "工作", "new": 2, "error": None}], "pending": 2,
                 "woke": True, "wake_error": None, "config_error": None}
    assert waker.calls == 1
    assert rows(hub_dir) == [
        (WORK, 4, 1, "pending", "to", "to me"), (WORK, 5, 1, "pending", "cc", "cc me")]
    assert state(hub_dir) == {"uidvalidity": 1, "last_uid": 5, "backfill_before": None}

    # 2 minutes later, nothing new: the batch was woken less than 10 minutes ago
    r = run(hub_dir, fake, minutes=2, wake=waker)
    assert (r["mailboxes"][0]["new"], r["pending"], r["woke"]) == (0, 2, False)
    assert waker.calls == 1

    # 11 minutes after the wake the batch is still pending: triage is woken again
    r = run(hub_dir, fake, minutes=11, wake=waker)
    assert (r["pending"], r["woke"]) == (2, True)
    assert waker.calls == 2
    with Store.open(hub_dir / "mail") as store:
        assert store.poll_times() == [
            "2026-10-14T04:11:00+00:00", "2026-10-14T04:02:00+00:00", "2026-10-14T04:00:00+00:00"]
        assert [m["subject"] for m in store.pending()["items"]] == ["to me", "cc me"]


def test_mail_judged_meanwhile_ends_the_batch(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    fake.deliver(mail_from("boss@corp.example"))
    waker = Waker()
    assert run(hub_dir, fake, wake=waker)["woke"] is True
    with Store.open(hub_dir / "mail") as store:
        store.judge({store.pending()["items"][0]["id"]: "important"}, t(1))
    r = run(hub_dir, fake, minutes=20, wake=waker)
    assert (r["pending"], r["woke"], waker.calls) == (0, False, 1)
    # the next mail starts a new batch and wakes at once
    fake.deliver(mail_from("boss@corp.example", "again"))
    r = run(hub_dir, fake, minutes=21, wake=waker)
    assert (r["pending"], r["woke"], waker.calls) == (1, True, 2)


def test_a_failed_wake_is_reported_and_retried_at_the_next_poll(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    fake.deliver(mail_from("boss@corp.example"))
    waker = Waker(error="cc-connect not found on PATH")
    r = run(hub_dir, fake, wake=waker)
    assert (r["woke"], r["wake_error"]) == (False, "cc-connect not found on PATH")
    waker.error = None
    r = run(hub_dir, fake, minutes=2, wake=waker)  # not marked: no 10-minute wait
    assert (r["woke"], r["wake_error"], waker.calls) == (True, None, 2)


def test_a_wake_that_raises_is_a_wake_error(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    fake.deliver(mail_from("boss@corp.example"))

    def broken():
        raise RuntimeError("boom")

    r = run(hub_dir, fake, wake=broken)
    assert r["woke"] is False and "boom" in r["wake_error"]


def test_without_wake_nothing_is_woken_and_the_poll_is_recorded(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    fake.deliver(mail_from("boss@corp.example"))
    r = run(hub_dir, fake)
    assert (r["pending"], r["woke"], r["wake_error"]) == (1, False, None)
    with Store.open(hub_dir / "mail") as store:
        assert store.poll_times() == ["2026-10-14T04:00:00+00:00"]
        assert store.batch_needs_wake(t(1)) is True  # a later poll with --wake wakes for it


# -- routing ---------------------------------------------------------------------------------


def test_routing_without_waking(hub_dir):
    mail = hub_dir / "mail"
    work, qq, shop = FakeServer(), FakeServer(), FakeServer()
    add(hub_dir, work)
    add(hub_dir, qq, "QQ", QQ, "qq")
    add(hub_dir, shop, "Shop", "me@163.com", "163")
    text = read_text(mail).replace("muted_senders = []", 'muted_senders = ["@ads.example"]', 1)
    write_text(mail, text)
    set_mailbox(hub_dir, "工作", "muted_senders", ["News@Shop.Example"])
    set_mailbox(hub_dir, "QQ", "delivery", "digest")
    set_mailbox(hub_dir, "Shop", "paused", True)

    work.deliver(mail_from("promo@ads.example", "sale"))  # user-level muted domain
    work.deliver(mail_from("PROMO@ADS.EXAMPLE", "SALE"))  # case does not matter
    work.deliver(mail_from("news@shop.example", "news"))  # mailbox-level muted address
    qq.deliver(mail_from("friend@qq.com", "dinner", to=QQ))  # delivery digest
    shop.deliver(mail_from("seller@163.com", "order", to="me@163.com"))  # paused mailbox
    waker = Waker()
    r = run(hub_dir, {"工作": work, "QQ": qq, "Shop": shop}, wake=waker)
    assert r["mailboxes"] == [
        {"name": "工作", "new": 3, "error": None}, {"name": "QQ", "new": 1, "error": None},
        {"name": "Shop", "new": 1, "error": None}]
    assert (r["pending"], r["woke"], waker.calls) == (0, False, 0)
    assert {row[3] for row in rows(hub_dir)} == {"digest"}
    with Store.open(mail) as store:
        assert store.digest_items()["total"] == 5

    # mail from anyone else in the instant mailbox is judged by the agent
    work.deliver(mail_from("boss@corp.example", "please reply"))
    work.deliver(mail_from("promo@notads.example", "not the muted domain"))
    work.deliver(mail_from("promo@ads.example.org", "not the muted domain either"))
    r = run(hub_dir, {"工作": work, "QQ": qq, "Shop": shop}, minutes=5, wake=waker)
    assert (r["pending"], r["woke"], waker.calls) == (3, True, 1)


@pytest.mark.parametrize("sender,muted,expected", [
    ("a@ads.example", ["@ads.example"], True),
    ("a@ADS.example", ["@ads.EXAMPLE"], True),
    ("a@sub.ads.example", ["@ads.example"], False),
    ("a@badads.example", ["@ads.example"], False),
    ("News@Shop.example", ["news@shop.example"], True),
    ("other@shop.example", ["news@shop.example"], False),
    ("news@shop.example", [" news@shop.example "], True),
    ("", ["@ads.example"], False),
    ("a@ads.example", [], False),
])
def test_is_muted(sender, muted, expected):
    assert poll.is_muted(sender, muted) is expected


# -- backfill, baselines and UIDVALIDITY -----------------------------------------------------


def test_backfill_is_old_and_chunked(hub_dir):
    fake = FakeServer()
    fake.deliver(mail_from("a@x.example", "ancient 1"), received=OLD)
    fake.deliver(mail_from("a@x.example", "ancient 2"), received=OLD)
    for i in range(450):
        fake.deliver(mail_from("a@x.example", f"recent {i}"), received=RECENT)
    add(hub_dir, fake)
    assert state(hub_dir) == {"uidvalidity": 1, "last_uid": 452, "backfill_before": 453}
    waker = Waker()

    r = run(hub_dir, fake, wake=waker)
    assert r["mailboxes"] == [{"name": "工作", "new": 0, "error": None}]
    indexed = rows(hub_dir)
    assert len(indexed) == 200 and {row[3] for row in indexed} == {"old"}
    assert [row[1] for row in indexed] == list(range(253, 453))  # the newest first
    assert state(hub_dir)["backfill_before"] == 253
    fetches = [call[1] for call in fake.calls if call[0] == "fetch"]
    assert fetches and all(len(uids) <= poll.FETCH_BATCH for uids in fetches)
    assert max(fetches[0]) == 452

    # mail that arrives during the backfill is new mail, never old
    fake.deliver(mail_from("boss@corp.example", "new one"))
    r = run(hub_dir, fake, minutes=5, wake=waker)
    assert r["mailboxes"][0]["new"] == 1 and r["pending"] == 1
    assert state(hub_dir) == {"uidvalidity": 1, "last_uid": 453, "backfill_before": 53}
    with Store.open(hub_dir / "mail") as store:  # triage judged it
        store.judge({store.pending()["items"][0]["id"]: "digest"}, t(6))

    calls = waker.calls
    run(hub_dir, fake, minutes=10, wake=waker)
    assert state(hub_dir) == {"uidvalidity": 1, "last_uid": 453, "backfill_before": None}
    old = [row for row in rows(hub_dir) if row[3] == "old"]
    assert [row[1] for row in old] == list(range(3, 453))  # all 450, the ancient two never
    assert waker.calls == calls  # backfill never wakes
    with Store.open(hub_dir / "mail") as store:
        assert store.pending()["total"] == 0
        assert store.digest_items()["total"] == 1  # only the judged one: old mail never

    # nothing left: the next poll does not search for it
    fake.calls.clear()
    run(hub_dir, fake, minutes=15)
    assert "uids_since" not in fake.names() and "fetch" not in fake.names()


def test_backfill_chunk_can_be_set(hub_dir):
    fake = FakeServer()
    for i in range(5):
        fake.deliver(mail_from("a@x.example", f"recent {i}"), received=RECENT)
    add(hub_dir, fake)
    run(hub_dir, fake, backfill_chunk=2)
    assert [row[1] for row in rows(hub_dir)] == [4, 5]
    assert state(hub_dir)["backfill_before"] == 4
    run(hub_dir, fake, minutes=5, backfill_chunk=3)  # the last three: done in one go
    assert [row[1] for row in rows(hub_dir)] == [1, 2, 3, 4, 5]
    assert state(hub_dir)["backfill_before"] is None


def test_uidvalidity_change_rebaselines_without_pushing(hub_dir):
    fake = FakeServer(uidvalidity=7)
    fake.deliver(mail_from("a@x.example", "recent 1"), received=RECENT)
    fake.deliver(mail_from("a@x.example", "recent 2"), received=RECENT)
    add(hub_dir, fake)
    waker = Waker()
    run(hub_dir, fake, wake=waker)
    assert [(row[1], row[2], row[3]) for row in rows(hub_dir)] == [(1, 7, "old"), (2, 7, "old")]

    # the server renumbers INBOX: the same two and three more, all under new UIDs
    fake.inbox.clear()
    fake.received.clear()
    fake.uidvalidity, fake.uidnext = 8, 1
    for i in range(1, 3):
        fake.deliver(mail_from("a@x.example", f"recent {i}"), received=RECENT)
    for i in range(3):
        fake.deliver(mail_from("boss@corp.example", f"unseen {i}"), received=NOW)
    r = run(hub_dir, fake, minutes=5, wake=waker)
    assert r["mailboxes"] == [{"name": "工作", "new": 0, "error": None}]
    assert (r["pending"], r["woke"], waker.calls) == (0, False, 0)
    assert state(hub_dir) == {"uidvalidity": 8, "last_uid": 5, "backfill_before": None}
    assert {row[3] for row in rows(hub_dir)} == {"old"}  # re-indexed as old, never pending
    assert sorted(row[1] for row in rows(hub_dir) if row[2] == 8) == [1, 2, 3, 4, 5]

    fake.deliver(mail_from("boss@corp.example", "really new"))
    r = run(hub_dir, fake, minutes=10, wake=waker)
    assert (r["mailboxes"][0]["new"], r["pending"], r["woke"]) == (1, 1, True)
    assert rows(hub_dir)[-1][1:4] == (6, 8, "pending")


def test_no_baseline_sets_one_and_takes_nothing_as_new(hub_dir):
    fake = FakeServer()
    fake.deliver(mail_from("a@x.example", "ancient"), received=OLD)
    fake.deliver(mail_from("a@x.example", "recent"), received=RECENT)
    add(hub_dir, fake)
    # the index is rebuilt on another machine: config and password are there, the index is not
    for name in os.listdir(hub_dir / "mail" / ".state"):
        os.remove(hub_dir / "mail" / ".state" / name)
    fake.deliver(mail_from("boss@corp.example", "came meanwhile"))
    waker = Waker()
    r = run(hub_dir, fake, wake=waker)
    assert r["mailboxes"] == [{"name": "工作", "new": 0, "error": None}]
    assert (r["pending"], r["woke"]) == (0, False)
    assert state(hub_dir) == {"uidvalidity": 1, "last_uid": 3, "backfill_before": None}
    assert [(row[1], row[3]) for row in rows(hub_dir)] == [(2, "old"), (3, "old")]
    with Store.open(hub_dir / "mail") as store:
        assert store.last_ok(WORK) == "2026-10-14T04:00:00+00:00"


def test_no_baseline_and_an_empty_inbox(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    os.remove(hub_dir / "mail" / ".state" / "mail.db")
    run(hub_dir, fake)
    assert state(hub_dir) == {"uidvalidity": 1, "last_uid": 0, "backfill_before": None}
    fake.deliver(mail_from("boss@corp.example"))
    assert run(hub_dir, fake, minutes=5)["pending"] == 1


# -- problems --------------------------------------------------------------------------------


def test_failing_mailbox_does_not_stop_others_and_reports_once(hub_dir):
    bad, good = FakeServer(), FakeServer()
    add(hub_dir, bad, "QQ", QQ, "qq")
    add(hub_dir, good)
    bad.fail_login = "imap-login"
    good.deliver(mail_from("boss@corp.example"))
    waker = Waker()
    servers = {"QQ": bad, "工作": good}

    r = run(hub_dir, servers, wake=waker)
    assert r["mailboxes"][0]["name"] == "QQ" and "refused the login" in r["mailboxes"][0]["error"]
    assert r["mailboxes"][1] == {"name": "工作", "new": 1, "error": None}
    assert (r["pending"], r["woke"], waker.calls) == (1, True, 1)
    found = problems(hub_dir, unreported_only=True)
    assert [(p["address"], p["kind"], p["state"]) for p in found] == [(QQ, "login", "open")]
    report_problems(hub_dir, 1)  # triage told the user

    for minutes in (3, 6):  # the same problem: no new entry, no new wake
        r = run(hub_dir, servers, minutes=minutes, wake=waker)
        assert r["mailboxes"][0]["error"]
    assert waker.calls == 1
    found = problems(hub_dir)
    assert len(found) == 1
    assert (found[0]["first_seen"], found[0]["last_seen"]) == (
        "2026-10-14T04:00:00+00:00", "2026-10-14T04:06:00+00:00")
    assert problems(hub_dir, unreported_only=True) == []

    bad.fail_login = None  # the user stored a new password
    r = run(hub_dir, servers, minutes=8, wake=waker)
    assert r["mailboxes"][0] == {"name": "QQ", "new": 0, "error": None}
    recovered = problems(hub_dir, unreported_only=True)
    assert [(p["address"], p["state"]) for p in recovered] == [(QQ, "recovered")]
    assert waker.calls == 1  # a recovery does not wake: it goes with the next message
    with Store.open(hub_dir / "mail") as store:  # triage judged the first batch
        store.judge({m["id"]: "digest" for m in store.pending()["items"]}, t(8.5))
    good.deliver(mail_from("boss@corp.example", "next"))
    r = run(hub_dir, servers, minutes=9, wake=waker)
    assert (r["woke"], waker.calls) == (True, 2)  # the next mail; triage lists the recovery
    report_problems(hub_dir, 9.2)
    run(hub_dir, servers, minutes=9.5, wake=waker)
    assert problems(hub_dir, unreported_only=True) == [] and waker.calls == 2
    with Store.open(hub_dir / "mail") as store:
        assert store.last_ok(QQ) == "2026-10-14T04:09:30+00:00"  # the last poll reached it


def test_a_flapping_mailbox_wakes_once(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    waker = Waker()
    for step in range(24):  # down, up, down, up... for two hours, a poll every 5 minutes
        fake.fail_login = "connect" if step % 2 == 0 else None
        r = run(hub_dir, fake, minutes=5 * step, wake=waker)
        assert r["woke"] is (step == 0)
        if step == 0:
            report_problems(hub_dir, 1)  # triage told the user
    assert waker.calls == 1
    assert {p["state"] for p in problems(hub_dir)} == {"recovered"}  # for the next message


def test_a_problem_woken_but_never_reported_does_not_wake_every_poll(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    fake.fail_login = "imap-login"
    waker = Waker()
    assert run(hub_dir, fake, wake=waker)["woke"] is True
    for minutes in range(5, 24 * 60, 30):  # triage failed to mark it, all day long
        assert run(hub_dir, fake, minutes=minutes, wake=waker)["woke"] is False
    assert waker.calls == 1
    assert problems(hub_dir, unreported_only=True)[0]["state"] == "open"
    assert run(hub_dir, fake, minutes=24 * 60, wake=waker)["woke"] is True  # once a day


def test_a_second_mailbox_problem_waits_ten_minutes(hub_dir):
    work, qq = FakeServer(), FakeServer()
    add(hub_dir, work)
    add(hub_dir, qq, "QQ", QQ, "qq")
    servers = {"工作": work, "QQ": qq}
    waker = Waker()
    work.fail_login = "imap-login"
    assert run(hub_dir, servers, wake=waker)["woke"] is True
    qq.fail_login = "connect"
    assert run(hub_dir, servers, minutes=5, wake=waker)["woke"] is False
    assert run(hub_dir, servers, minutes=10, wake=waker)["woke"] is True
    assert waker.calls == 2
    assert run(hub_dir, servers, minutes=20, wake=waker)["woke"] is False


def test_a_failed_problem_wake_is_retried(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    fake.fail_login = "imap-login"
    waker = Waker(error="cc-connect is not running")
    r = run(hub_dir, fake, wake=waker)
    assert (r["woke"], r["wake_error"]) == (False, "cc-connect is not running")
    waker.error = None
    assert run(hub_dir, fake, minutes=5, wake=waker)["woke"] is True
    assert run(hub_dir, fake, minutes=20, wake=waker)["woke"] is False
    assert waker.calls == 2


def test_a_mail_wake_carries_the_problems(hub_dir):
    work, qq = FakeServer(), FakeServer()
    add(hub_dir, work)
    add(hub_dir, qq, "QQ", QQ, "qq")
    qq.fail_login = "imap-login"
    work.deliver(mail_from("boss@corp.example"))
    waker = Waker()
    assert run(hub_dir, {"工作": work, "QQ": qq}, wake=waker)["woke"] is True
    with Store.open(hub_dir / "mail") as store:  # triage judged the mail, but did not mark
        store.judge({m["id"]: "digest" for m in store.pending()["items"]}, t(1))
    assert run(hub_dir, {"工作": work, "QQ": qq}, minutes=15, wake=waker)["woke"] is False
    assert waker.calls == 1


@pytest.mark.parametrize("setup,kind,words", [
    ({"fail_login": "imap-login"}, "login", "refused the login"),
    ({"fail_login": "connect"}, "connect", "cannot reach"),
    ({"error": ServerError("connection reset by peer")}, "connect", "connection reset"),
])
def test_problem_kinds(hub_dir, setup, kind, words):
    fake = FakeServer()
    add(hub_dir, fake)
    for key, value in setup.items():
        setattr(fake, key, value)
    r = run(hub_dir, fake)
    assert words in r["mailboxes"][0]["error"]
    assert [(p["address"], p["kind"]) for p in problems(hub_dir)] == [(WORK, kind)]
    assert words in problems(hub_dir)[0]["message"]


def test_a_missing_password_is_a_login_problem(hub_dir):
    work, qq = FakeServer(), FakeServer()
    add(hub_dir, work)
    add(hub_dir, qq, "QQ", QQ, "qq")
    accounts.secret_path(hub_dir, WORK).unlink()
    qq.deliver(mail_from("friend@qq.com", to=QQ))
    calls = len(work.calls)
    r = run(hub_dir, {"工作": work, "QQ": qq})
    assert "no password stored" in r["mailboxes"][0]["error"]
    assert r["mailboxes"][1] == {"name": "QQ", "new": 1, "error": None}
    assert [(p["address"], p["kind"]) for p in problems(hub_dir)] == [(WORK, "login")]
    assert len(work.calls) == calls  # not even connected


def test_a_problem_message_never_holds_the_password(hub_dir):
    class Echo(FakeServer):
        def inbox_status(self):
            raise LoginError("imap-login", f"bad password {self.given_password}")

    add(hub_dir, FakeServer(), password="s3cret-Pw")
    r = run(hub_dir, Echo())
    assert "s3cret-Pw" not in r["mailboxes"][0]["error"]
    assert "s3cret-Pw" not in problems(hub_dir)[0]["message"]
    assert "[password]" in problems(hub_dir)[0]["message"]


def test_an_unexpected_error_in_one_mailbox_does_not_stop_the_others(hub_dir):
    class Broken(FakeServer):
        def uids_after(self, uid):
            raise RuntimeError("a bug")

    broken, good = Broken(), FakeServer()
    add(hub_dir, FakeServer(), "QQ", QQ, "qq")
    add(hub_dir, good)
    good.deliver(mail_from("boss@corp.example"))
    r = run(hub_dir, {"QQ": broken, "工作": good})
    assert "RuntimeError" in r["mailboxes"][0]["error"] and "a bug" in r["mailboxes"][0]["error"]
    assert r["mailboxes"][1] == {"name": "工作", "new": 1, "error": None}
    assert [(p["address"], p["kind"]) for p in problems(hub_dir)] == [(QQ, "error")]
    assert broken.closed


def test_a_dropped_connection_mid_poll_neither_loses_nor_duplicates(hub_dir):
    class Flaky(FakeServer):
        fail_on = 2  # the n-th fetch fails; 0: none

        def fetch(self, uids):
            fetches = sum(1 for call in self.calls if call[0] == "fetch")
            if self.fail_on and fetches + 1 == self.fail_on:
                self.calls.append(("fetch", tuple(uids)))
                raise ServerError("connection reset")
            return super().fetch(uids)

    fake = Flaky()
    add(hub_dir, fake)
    for i in range(30):
        fake.deliver(mail_from("boss@corp.example", f"mail {i}"))
    r = run(hub_dir, fake)
    assert "connection reset" in r["mailboxes"][0]["error"]
    assert r["mailboxes"][0]["new"] == poll.FETCH_BATCH  # what was stored before the drop
    assert r["pending"] == poll.FETCH_BATCH
    assert [row[1] for row in rows(hub_dir)] == list(range(1, poll.FETCH_BATCH + 1))
    assert state(hub_dir)["last_uid"] == poll.FETCH_BATCH
    fake.fail_on = 0
    r = run(hub_dir, fake, minutes=5)
    assert r["mailboxes"][0] == {"name": "工作", "new": 30 - poll.FETCH_BATCH, "error": None}
    assert [row[1] for row in rows(hub_dir)] == list(range(1, 31))
    assert {row[3] for row in rows(hub_dir)} == {"pending"}
    assert state(hub_dir)["last_uid"] == 30


def test_a_crash_after_storing_but_before_advancing_does_not_duplicate(hub_dir, monkeypatch):
    fake = FakeServer()
    add(hub_dir, fake)
    for i in range(3):
        fake.deliver(mail_from("boss@corp.example", f"mail {i}"))
    real = Store.set_baseline

    def crash(self, *args):
        raise KeyboardInterrupt  # the process is killed between the two writes

    monkeypatch.setattr(Store, "set_baseline", crash)
    with pytest.raises(KeyboardInterrupt):
        run(hub_dir, fake)
    monkeypatch.setattr(Store, "set_baseline", real)
    assert state(hub_dir)["last_uid"] == 0 and len(rows(hub_dir)) == 3
    run(hub_dir, fake, minutes=5)
    assert [row[1] for row in rows(hub_dir)] == [1, 2, 3]
    assert state(hub_dir)["last_uid"] == 3


class Gappy(FakeServer):
    """A server whose FETCH reply leaves out the UIDs in `drop`: `times` times each (None:
    every time)."""

    def __init__(self, *args, drop=(), times=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.drop = set(drop)
        self.times = times
        self.dropped: dict[int, int] = {}

    def fetch(self, uids):
        out = []
        for uid, raw in super().fetch(uids):
            if uid in self.drop and (self.times is None or self.dropped.get(uid, 0) < self.times):
                self.dropped[uid] = self.dropped.get(uid, 0) + 1
                continue
            out.append((uid, raw))
        return out


def test_a_uid_left_out_of_a_fetch_once_is_taken_at_the_next_poll(hub_dir):
    fake = Gappy(drop={3}, times=1)
    add(hub_dir, fake)
    for i in range(5):
        fake.deliver(mail_from("boss@corp.example", f"mail {i + 1}"))
    r = run(hub_dir, fake)
    assert r["mailboxes"][0] == {"name": "工作", "new": 2, "error": None}
    assert [row[1] for row in rows(hub_dir)] == [1, 2]  # stopped before the gap
    assert state(hub_dir)["last_uid"] == 2
    r = run(hub_dir, fake, minutes=5)
    assert r["mailboxes"][0]["new"] == 3
    assert [(row[1], row[5]) for row in rows(hub_dir)] == [
        (i, f"mail {i}") for i in range(1, 6)]  # 3 is the real mail: nothing unreadable
    assert state(hub_dir)["last_uid"] == 5
    with Store.open(hub_dir / "mail") as store:
        assert store.missed(WORK) is None


def test_a_uid_left_out_of_every_fetch_becomes_an_unreadable_message(hub_dir):
    fake = Gappy(drop={22})
    add(hub_dir, fake)
    for i in range(25):
        fake.deliver(mail_from("boss@corp.example", f"mail {i + 1}"))
    r = run(hub_dir, fake)
    assert r["mailboxes"][0]["new"] == 21  # a whole batch of 20, then 21 before the gap
    assert state(hub_dir)["last_uid"] == 21
    r = run(hub_dir, fake, minutes=5)
    assert r["mailboxes"][0] == {"name": "工作", "new": 4, "error": None}
    subjects = {row[1]: (row[3], row[5]) for row in rows(hub_dir)}
    assert subjects[22] == ("pending", poll.UNREADABLE)  # never silent
    assert subjects[23] == ("pending", "mail 23") and len(subjects) == 25
    assert state(hub_dir)["last_uid"] == 25
    assert run(hub_dir, fake, minutes=10)["mailboxes"][0]["new"] == 0
    assert len(rows(hub_dir)) == 25
    with Store.open(hub_dir / "mail") as store:
        assert store.missed(WORK) is None


def test_a_uid_expunged_between_search_and_fetch_is_simply_gone(hub_dir):
    class Expunging(FakeServer):
        def uids_after(self, uid):
            found = super().uids_after(uid)
            self.inbox.pop(2, None)  # another client deletes it right after the search
            return found

    fake = Expunging()
    add(hub_dir, fake)
    for i in range(3):
        fake.deliver(mail_from("boss@corp.example", f"mail {i + 1}"))
    run(hub_dir, fake)
    assert [row[1] for row in rows(hub_dir)] == [1] and state(hub_dir)["last_uid"] == 1
    run(hub_dir, fake, minutes=5)  # no longer listed: the poll moves on
    assert [(row[1], row[5]) for row in rows(hub_dir)] == [(1, "mail 1"), (3, "mail 3")]
    assert state(hub_dir)["last_uid"] == 3
    with Store.open(hub_dir / "mail") as store:
        assert store.missed(WORK) is None


def test_a_new_uidvalidity_forgets_a_missed_uid(hub_dir):
    fake = Gappy(drop={2})
    add(hub_dir, fake)
    fake.deliver(mail_from("boss@corp.example", "one"))
    fake.deliver(mail_from("boss@corp.example", "two"))
    run(hub_dir, fake)
    with Store.open(hub_dir / "mail") as store:
        assert store.missed(WORK) == 2
    fake.uidvalidity = 9
    run(hub_dir, fake, minutes=5)
    with Store.open(hub_dir / "mail") as store:
        assert store.missed(WORK) is None


def test_a_broken_config_is_one_problem_and_wakes_once(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    mail = hub_dir / "mail"
    good = read_text(mail)
    write_text(mail, good.replace('delivery = "instant"', 'delivery = "sometimes"', 1))
    waker = Waker()
    calls = len(fake.calls)
    r = run(hub_dir, fake, wake=waker)
    assert r["mailboxes"] == [] and "delivery" in r["config_error"]
    assert (r["woke"], waker.calls) == (True, 1)
    assert [(p["address"], p["kind"]) for p in problems(hub_dir)] == [("config", "config")]
    assert len(fake.calls) == calls  # no mailbox is polled
    report_problems(hub_dir, 1)
    r = run(hub_dir, fake, minutes=5, wake=waker)
    assert r["config_error"] and waker.calls == 1 and len(problems(hub_dir)) == 1
    with Store.open(mail) as store:
        assert len(store.poll_times()) == 2

    write_text(mail, good)
    r = run(hub_dir, fake, minutes=10, wake=waker)
    assert r["config_error"] is None and r["mailboxes"][0]["error"] is None
    assert [(p["address"], p["state"]) for p in problems(hub_dir, unreported_only=True)] == [
        ("config", "recovered")]
    assert waker.calls == 1  # the recovery waits for the next message


def test_two_sections_with_one_address_are_polled_once(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    mail = hub_dir / "mail"
    box = accounts.config.load(mail).mailboxes["工作"]
    box.name = "工作 copy"
    write_text(mail, accounts.config.add_mailbox_text(read_text(mail), box))
    fake.deliver(mail_from("boss@corp.example"))
    fake.calls.clear()
    r = run(hub_dir, fake)
    assert [m["name"] for m in r["mailboxes"]] == ["工作"]
    assert fake.names().count("inbox_status") == 1
    assert r["pending"] == 1


# -- the server stays as it was --------------------------------------------------------------


def test_background_poll_never_changes_server_state(hub_dir):
    fake = FakeServer()
    for i in range(3):
        fake.deliver(mail_from("a@x.example", f"recent {i}"), received=RECENT,
                     flags=("\\Flagged",) if i == 1 else ())
    add(hub_dir, fake)
    fake.deliver(mail_from("boss@corp.example", "new"))
    before = ({uid: set(f) for uid, f in fake.flags.items()}, dict(fake.inbox))
    fake.calls.clear()
    run(hub_dir, fake, wake=Waker())
    run(hub_dir, fake, minutes=5, wake=Waker())
    assert fake.writes() == []
    assert set(fake.names()) <= {"inbox_status", "uids_after", "uids_since", "fetch", "close"}
    assert ({uid: set(f) for uid, f in fake.flags.items()}, dict(fake.inbox)) == before
    assert all("\\Seen" not in f for f in fake.flags.values())
    assert fake.closed


def test_bad_message_does_not_stop_the_mailbox(hub_dir, monkeypatch):
    fake = FakeServer()
    add(hub_dir, fake)
    fake.deliver(mail_from("boss@corp.example", "first"))
    fake.deliver(b"")  # nothing at all
    fake.deliver(b"\x00\x01\x02\xff\xfe garbage \xd5\xae" * 40)  # no headers, bytes only
    fake.deliver(b"Subject: HOSTILE\r\n\r\nmarker")
    fake.deliver(mail_from("boss@corp.example", "last"))
    real = message._parse

    def gives_up(raw):
        if b"HOSTILE" in raw:
            raise RecursionError("too deep")
        return real(raw)

    monkeypatch.setattr(message, "_parse", gives_up)
    r = run(hub_dir, fake)
    assert r["mailboxes"] == [{"name": "工作", "new": 5, "error": None}]
    subjects = {row[1]: row[5] for row in rows(hub_dir)}
    assert subjects == {
        1: "first", 2: poll.UNREADABLE, 3: "", 4: poll.UNREADABLE, 5: "last"}
    assert state(hub_dir)["last_uid"] == 5
    with Store.open(hub_dir / "mail") as store:
        garbage = next(m for m in store.pending()["items"] if m["uid"] == 3)
        assert "garbage" in garbage["text"]


# -- the lock --------------------------------------------------------------------------------


def test_a_second_poll_is_skipped_while_one_runs(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    fake.deliver(mail_from("boss@corp.example"))
    fake.calls.clear()
    waker = Waker()
    with poll.poll_lock(hub_dir / "mail") as held:
        assert held is True
        with poll.poll_lock(hub_dir / "mail") as again:
            assert again is False
        r = run(hub_dir, fake, wake=waker)
        assert r == {"skipped": "another poll is running"}
    assert fake.calls == [] and waker.calls == 0 and rows(hub_dir) == []
    with Store.open(hub_dir / "mail") as store:
        assert store.poll_times() == []
    assert run(hub_dir, fake, wake=waker)["woke"] is True


def test_the_lock_is_held_across_processes_and_dies_with_its_holder(hub_dir):
    add(hub_dir, FakeServer())
    lock = hub_dir / "mail" / ".state" / poll.LOCK_FILE
    code = (
        "import sqlite3, sys\n"
        f"db = sqlite3.connect({str(lock)!r}, timeout=0, isolation_level=None)\n"
        "db.execute('BEGIN EXCLUSIVE')\n"
        "print('locked', flush=True)\n"
        "sys.stdin.read()\n")
    holder = subprocess.Popen(
        [sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        assert run(hub_dir, FakeServer()) == {"skipped": "another poll is running"}
    finally:
        holder.kill()  # a poll that crashed: no stale lock is left
        holder.wait(timeout=30)
        holder.stdin.close()
        holder.stdout.close()
    for _ in range(50):  # the OS frees the lock of a dead process, on Windows a moment later
        r = run(hub_dir, FakeServer())
        if "skipped" not in r:
            break
        time.sleep(0.1)
    assert "skipped" not in r


def test_a_damaged_lock_file_is_replaced(hub_dir):
    add(hub_dir, FakeServer())
    lock = hub_dir / "mail" / ".state" / poll.LOCK_FILE
    lock.write_bytes(b"not a database at all" * 50)
    assert "skipped" not in run(hub_dir, FakeServer())


# -- waking triage through cc-hub ------------------------------------------------------------


@pytest.fixture
def fake_cchub(tmp_path, monkeypatch):
    """make(exit_code=0, stderr="") puts a fake `cc-hub` first on PATH; it records its
    arguments and working directory as JSON in the returned log file."""

    def make(exit_code: int = 0, stderr: str = ""):
        folder = tmp_path / "bin"
        folder.mkdir(exist_ok=True)
        log = tmp_path / "cc-hub.log.json"
        script = folder / "fake_cc_hub.py"
        script.write_text(
            "import json, os, sys\n"
            f"with open({str(log)!r}, 'w', encoding='utf-8') as f:\n"
            "    json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd()}, f)\n"
            f"sys.stderr.write({stderr!r})\n"
            f"sys.exit({exit_code})\n", "utf-8", newline="\n")
        if sys.platform == "win32":
            exe = folder / "cc-hub.cmd"
            exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n', "utf-8", newline="")
        else:
            exe = folder / "cc-hub"
            exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', "utf-8")
            exe.chmod(0o755)
        monkeypatch.setenv("PATH", os.pathsep.join([str(folder), os.environ.get("PATH", "")]))
        return log

    return make


def test_wake_triage_runs_cc_hub_cron_wake(fake_cchub, hub_dir):
    log = fake_cchub()
    assert poll.wake_triage(hub_dir) is None
    seen = json.loads(log.read_text("utf-8"))
    assert seen["argv"] == ["cron", "wake", "mail/triage"]
    assert os.path.samefile(seen["cwd"], hub_dir)


def test_wake_triage_failure_is_the_error_text(fake_cchub):
    fake_cchub(exit_code=1, stderr="hub:mail/triage is turned off; cc-hub cron set mail/triage --on\n")
    assert poll.wake_triage() == (
        "hub:mail/triage is turned off; cc-hub cron set mail/triage --on")
    fake_cchub(exit_code=3)
    assert "3" in poll.wake_triage()


def test_wake_triage_without_cc_hub(monkeypatch):
    monkeypatch.setattr(poll.shutil, "which", lambda name: None)
    assert poll.wake_triage() == "cc-hub not found"


def test_wake_triage_timeout(monkeypatch):
    monkeypatch.setattr(poll.shutil, "which", lambda name: "/usr/bin/cc-hub")
    seen = {}

    def slow(args, **options):
        seen.update(options, args=args)
        raise subprocess.TimeoutExpired(args, options["timeout"])

    monkeypatch.setattr(poll.subprocess, "run", slow)
    assert "60" in poll.wake_triage()
    assert seen["args"] == ["/usr/bin/cc-hub", "cron", "wake", "mail/triage"]
    assert seen["timeout"] == 60


# -- the command line ------------------------------------------------------------------------


@pytest.fixture
def mk(hub_dir, monkeypatch, capsys):
    fake = FakeServer()
    monkeypatch.setattr(server, "connect", fake_connect(fake))
    woken = []

    def fake_wake(hub=None):
        woken.append(hub)  # and the wake worked: None

    monkeypatch.setattr(poll, "wake_triage", fake_wake)

    def run_mk(*args):
        argv = ["--mail-dir", str(hub_dir / "mail"), "--hub", str(hub_dir), *args]
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        status = cli.main(argv)
        out, err = capsys.readouterr()
        return status, json.loads(out) if out.strip() else out, err

    run_mk.fake = fake
    run_mk.woken = woken
    return run_mk


def test_cli_bg_poll_wake(hub_dir, mk):
    add(hub_dir, mk.fake)
    mk.fake.deliver(mail_from("boss@corp.example"))
    status, out, err = mk("bg", "poll", "--wake")
    assert (status, err) == (0, "")
    assert out["mailboxes"] == [{"name": "工作", "new": 1, "error": None}]
    assert (out["pending"], out["woke"], out["wake_error"]) == (1, True, None)
    assert mk.woken == [hub_dir]
    with Store.open(hub_dir / "mail") as store:
        assert len(store.poll_times()) == 1


def test_cli_bg_poll_without_wake(hub_dir, mk):
    add(hub_dir, mk.fake)
    mk.fake.deliver(mail_from("boss@corp.example"))
    status, out, _ = mk("bg", "poll")
    assert status == 0 and out["pending"] == 1 and out["woke"] is False
    assert mk.woken == []


def test_cli_bg_poll_skipped(hub_dir, mk):
    add(hub_dir, mk.fake)
    with poll.poll_lock(hub_dir / "mail"):
        status, out, _ = mk("bg", "poll", "--wake")
    assert status == 0 and out == {"skipped": "another poll is running"}
    assert mk.woken == []

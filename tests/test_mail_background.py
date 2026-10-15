"""The background commands (`mk bg ...`): their JSON shapes on an index filled by real polls
with FakeServers, paging, judging, searching (local and on the server), problems, the digest
and the status check."""

import io
import json
import shutil
import sqlite3
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from mailfakes import FakeServer, fake_connect, make_raw
from mailkit import accounts, background, cli, config, poll, server
from mailkit.config import MailError, read_text, set_mailbox_value_text, write_text
from mailkit.message import parse
from mailkit.server import ServerError
from mailkit.store import Store

SEED_MAIL = Path(__file__).parents[1] / "mail" / "seed" / "mail"
NOW = datetime(2026, 10, 14, 4, 0, tzinfo=UTC)
WORK = "me@corp.example"
QQ = "me@qq.com"
OLD = NOW - timedelta(days=200)  # before the 90 days that are backfilled
PASSWORD = "s3cretpass"
# whether the wakes reach triage, in `bg status` and `bg digest-items`
WAKE_KEYS = ("last_wake_at", "wake_error", "wake_error_since", "unacknowledged_wakes",
             "oldest_pending_at", "oldest_pending_minutes")


def t(minutes: float = 0) -> datetime:
    return NOW + timedelta(minutes=minutes)


@pytest.fixture
def hub_dir(tmp_path):
    hub = tmp_path / "hub"
    shutil.copytree(SEED_MAIL, hub / "mail")
    (hub / "secrets").mkdir()
    return hub


def mail(hub):
    return hub / "mail"


def add(hub, fake, name="工作", address=WORK, provider="feishu", password=PASSWORD):
    r = accounts.add(hub, mail(hub), name, address, password, provider=provider,
                     connect=fake_connect(fake), now=NOW)
    assert r["ok"], r


def two_boxes(hub):
    """Mailboxes 工作 (WORK) and QQ, each on its own FakeServer."""
    work, qq = FakeServer(), FakeServer()
    add(hub, work)
    add(hub, qq, "QQ", QQ, "qq")
    return work, qq


def run(hub, servers, minutes: float = 0, **options):
    """A run of the poll task (`background`), unless the test says otherwise."""
    options.setdefault("background", True)
    return poll.poll(hub, mail(hub), connect=fake_connect(servers), now=t(minutes), **options)


def set_mailbox(hub, name, key, value):
    write_text(mail(hub), set_mailbox_value_text(read_text(mail(hub)), name, key, value))


def mail_from(sender, subject="hi", to=WORK, body=None, **options):
    return make_raw(subject, sender, to, body or f"body of {subject}", **options)


def pending_ids(hub):
    with Store.open(mail(hub)) as store:
        return [m["id"] for m in store.pending(limit=1000)["items"]]


def judged(hub, decisions, minutes=1):
    """Give verdicts: `{id: decision}` through the background command."""
    return background.judge(mail(hub), [f"{i}={d}" for i, d in decisions.items()], now=t(minutes))


def subjects(result):
    return [item["subject"] for item in result["items"]]


# -- pending ---------------------------------------------------------------------------------


def test_pending_shape(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    long_body = "x" * 5000
    attached = make_raw("Invoice", "Billing <bill@shop.example>", WORK, long_body,
                        attachments=[("a.pdf", b"%PDF-1.4 12345", "application/pdf")])
    fake.deliver(attached)
    fake.deliver(mail_from("boss@corp.example", "cc me", to="team@corp.example", cc=WORK))
    fake.deliver(mail_from("news@x.example", "weekly", to="list@x.example",
                           headers={"Precedence": "bulk"}))
    run(hub_dir, fake)

    out = background.pending(mail(hub_dir))
    assert set(out) == {"rules", "config_error", "items", "total", "next_page", "problems",
                        "problems_total"}
    settings = config.load(mail(hub_dir))
    assert out["rules"] == {"工作": config.effective(settings, "工作")}
    assert (out["total"], out["next_page"], out["problems"], out["problems_total"],
            out["config_error"]) == (3, None, [], 0, None)
    first, second, third = out["items"]
    keys = {"id", "mailbox", "from", "from_name", "addressed", "subject", "date", "excerpt",
            "attachments", "bulk"}
    assert set(first) == keys == set(second) == set(third)
    assert (first["mailbox"], first["from"], first["from_name"], first["addressed"],
            first["subject"], first["bulk"]) == ("工作", "bill@shop.example", "Billing", "to",
                                                  "Invoice", False)
    assert first["excerpt"] == parse(attached).text[:2000]
    assert len(first["excerpt"]) == 2000
    assert first["attachments"] == [{"index": 0, "filename": "a.pdf", "size": 14}]
    assert first["date"] == "2026-10-14T01:00:00+00:00"
    assert (second["addressed"], second["attachments"]) == ("cc", [])
    assert third["bulk"] is True
    json.dumps(out, ensure_ascii=False)  # plain JSON data


def test_pending_pages_at_ten(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    for i in range(25):
        fake.deliver(mail_from("boss@corp.example", f"m{i:02d}"))
    run(hub_dir, fake)

    one = background.pending(mail(hub_dir))
    assert (len(one["items"]), one["total"], one["next_page"]) == (10, 25, 2)
    two = background.pending(mail(hub_dir), page=2)
    three = background.pending(mail(hub_dir), page=3)
    assert (len(two["items"]), two["next_page"]) == (10, 3)
    assert (len(three["items"]), three["next_page"], three["total"]) == (5, None, 25)
    assert subjects(one) + subjects(two) + subjects(three) == [f"m{i:02d}" for i in range(25)]
    assert background.pending(mail(hub_dir), page=4)["items"] == []
    with pytest.raises(MailError):
        background.pending(mail(hub_dir), page=0)


def test_pending_rules_are_the_effective_rules_of_each_mailbox(hub_dir):
    work, qq = two_boxes(hub_dir)
    set_mailbox(hub_dir, "QQ", "important", "only mail from my mother")
    set_mailbox(hub_dir, "QQ", "muted_senders", ["spam@x.example"])
    work.deliver(mail_from("a@x.example"))
    run(hub_dir, {WORK: work, QQ: qq})
    out = background.pending(mail(hub_dir))
    settings = config.load(mail(hub_dir))
    assert out["rules"] == {"工作": config.effective(settings, "工作"),
                            "QQ": config.effective(settings, "QQ")}
    assert out["rules"]["QQ"]["important"] == "only mail from my mother"


def test_pending_lists_the_unreported_problems_only(hub_dir):
    work, qq = two_boxes(hub_dir)
    qq.fail_login = "imap-login"
    work.deliver(mail_from("a@x.example"))
    run(hub_dir, {WORK: work, QQ: qq})
    out = background.pending(mail(hub_dir))
    assert [(p["mailbox"], p["kind"], p["state"]) for p in out["problems"]] == [
        ("QQ", "login", "open")]
    with Store.open(mail(hub_dir)) as store:
        store.mark_reported([p["id"] for p in store.problems(unreported_only=True)], t(1))
    assert background.pending(mail(hub_dir))["problems"] == []


@pytest.mark.parametrize("how", ["broken", "missing"])
def test_pending_without_a_usable_config_still_answers(hub_dir, how):
    fake = FakeServer()
    add(hub_dir, fake)
    fake.deliver(mail_from("boss@corp.example", "needs you"))
    run(hub_dir, fake)
    path = mail(hub_dir) / "config.toml"
    if how == "broken":
        path.write_text("this is [not toml", encoding="utf-8")
    else:
        path.unlink()
    run(hub_dir, fake, minutes=5)  # the poll notes the config problem

    out = background.pending(mail(hub_dir))
    assert out["rules"] == {}
    assert out["config_error"] and "config.toml" in out["config_error"]
    assert [p["kind"] for p in out["problems"]] == ["config"]
    assert out["problems"][0]["mailbox"] is None
    assert subjects(out) == ["needs you"]
    assert out["items"][0]["mailbox"] == WORK  # no name to give: the address


# -- judge -----------------------------------------------------------------------------------


def test_judge_records_verdicts_and_counts(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    for i in range(3):
        fake.deliver(mail_from("boss@corp.example", f"m{i}"))
    run(hub_dir, fake)
    a, b, c = pending_ids(hub_dir)

    assert judged(hub_dir, {a: "important", b: "digest"}) == {
        "judged": 2, "important": [a], "digest": [b], "already_judged": [], "pending_left": 1}
    with Store.open(mail(hub_dir)) as store:
        assert store.message(a)["status"] == "important"
        assert store.message(b)["status"] == "digest"
        assert store.message(c)["status"] == "pending"
        assert [p["id"] for p in store.pushes()] == [a]
    # a retried run, or another run that judged it first: judged mail is left alone
    assert judged(hub_dir, {a: "digest"}) == {
        "judged": 0, "important": [], "digest": [], "already_judged": [a], "pending_left": 1}
    assert judged(hub_dir, {c: "digest"}) == {
        "judged": 1, "important": [], "digest": [c], "already_judged": [], "pending_left": 0}


def test_judge_of_a_run_that_overlaps_another_names_only_its_own_pushes(hub_dir):
    """The digest judges while a triage woken by the same poll does: each pushes only the ids
    in its own `important`."""
    fake = FakeServer()
    add(hub_dir, fake)
    for i in range(3):
        fake.deliver(mail_from("boss@corp.example", f"m{i}"))
    run(hub_dir, fake)
    a, b, c = pending_ids(hub_dir)
    triage = judged(hub_dir, {a: "important", b: "important"}, minutes=1)
    digest = judged(hub_dir, {a: "important", b: "digest", c: "important"}, minutes=2)
    assert (triage["important"], triage["already_judged"]) == ([a, b], [])
    assert (digest["important"], digest["digest"], digest["already_judged"]) == ([c], [], [a, b])
    with Store.open(mail(hub_dir)) as store:
        assert sorted(p["id"] for p in store.pushes()) == [a, b, c]  # one push each


def test_judge_refuses_unknown_ids_and_decisions_and_changes_nothing(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    fake.deliver(mail_from("boss@corp.example"))
    run(hub_dir, fake)
    (a,) = pending_ids(hub_dir)
    with pytest.raises(MailError, match="no message with id 9999"):
        background.judge(mail(hub_dir), [f"{a}=important", "9999=digest"], now=t(1))
    with pytest.raises(MailError, match="important, digest"):
        background.judge(mail(hub_dir), [f"{a}=urgent"], now=t(1))
    with pytest.raises(MailError):
        background.judge(mail(hub_dir), [f"{a}=Important"], now=t(1))
    assert pending_ids(hub_dir) == [a]
    with Store.open(mail(hub_dir)) as store:
        assert store.pushes() == []


@pytest.mark.parametrize("token", ["3", "=digest", "x=digest", "3=", "3==digest", "-1=digest",
                                   "3 =digest", "3=digest;4=digest", ""])
def test_judge_refuses_a_token_that_is_not_id_equals_decision(hub_dir, token):
    with pytest.raises(MailError, match="ID=DECISION"):
        background.judge(mail(hub_dir), [token], now=t(1))


def test_judge_refuses_one_id_with_two_decisions(hub_dir):
    with pytest.raises(MailError, match="two different decisions"):
        background.judge(mail(hub_dir), ["3=digest", "3=important"], now=t(1))
    with pytest.raises(MailError):
        background.judge(mail(hub_dir), [], now=t(1))


def test_judge_takes_one_id_given_twice_with_the_same_decision(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    fake.deliver(mail_from("boss@corp.example"))
    run(hub_dir, fake)
    (a,) = pending_ids(hub_dir)
    assert judged(hub_dir, {a: "digest"})["judged"] == 1
    again = background.judge(mail(hub_dir), [f"{a}=digest", f"{a}=digest"], now=t(2))
    assert (again["judged"], again["already_judged"]) == (0, [a])


# -- read ------------------------------------------------------------------------------------


def test_read_returns_the_whole_message(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    body = "".join(f"line {i} of the letter\n" for i in range(400))
    raw = make_raw("Long letter", "Ann <ann@x.example>", WORK, body, cc="bob@x.example",
                   attachments=[("报告.pdf", b"%PDF-1.4 abc", "application/pdf")],
                   headers={"In-Reply-To": "<prev@x.example>"})
    fake.deliver(raw)
    run(hub_dir, fake)
    (mid,) = pending_ids(hub_dir)

    out = background.read(mail(hub_dir), mid)
    parsed = parse(raw)
    assert out["text"] == parsed.text and len(out["text"]) > 2000
    assert (out["id"], out["mailbox"], out["address"], out["from"], out["from_name"],
            out["subject"], out["status"]) == (
        mid, "工作", WORK, "ann@x.example", "Ann", "Long letter", "pending")
    assert out["to"] == [WORK] and out["cc"] == ["bob@x.example"]
    assert out["in_reply_to"] == "<prev@x.example>"
    assert out["message_id"] == parsed.message_id
    assert out["attachments"] == [
        {"index": 0, "filename": "报告.pdf", "content_type": "application/pdf", "size": 12}]
    for key in ("date", "addressed", "bulk", "references", "added_at"):
        assert key in out
    assert (out["moved_to"], out["moved_at"], out["moved_expunged"]) == (None, None, None)
    json.dumps(out, ensure_ascii=False)


def test_read_unknown_id_is_an_error(hub_dir):
    with pytest.raises(MailError, match="no message with id 5"):
        background.read(mail(hub_dir), 5)


def test_read_names_the_address_when_the_config_is_gone(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    fake.deliver(mail_from("boss@corp.example"))
    run(hub_dir, fake)
    (mid,) = pending_ids(hub_dir)
    (mail(hub_dir) / "config.toml").unlink()
    assert background.read(mail(hub_dir), mid)["mailbox"] == WORK


# -- search (local) --------------------------------------------------------------------------


def old_mail(fake, subject, days, sender="tax@gov.example", to=WORK, body=None):
    """Mail that arrived `days` ago (its Date header and the server's internal date)."""
    when = NOW - timedelta(days=days)
    return fake.deliver(
        mail_from(sender, subject, to=to, body=body or f"about {subject}", date=when),
        received=when)


def indexed_mail(hub):
    """Two mailboxes whose history (INBOX mail from before they were added) the first poll
    indexes as `old`."""
    work, qq = FakeServer(), FakeServer()
    old_mail(work, "Your invoice", 20, sender="shop@store.example", body="invoice 42 is due")
    old_mail(work, "Lunch?", 5, sender="ann@x.example", body="shall we eat")
    old_mail(qq, "Receipt", 3, sender="shop@store.example", to=QQ, body="your invoice receipt")
    old_mail(qq, "周末回家吗", 1, sender="mom@x.example", to=QQ, body="周末回家吃饭")
    add(hub, work)
    add(hub, qq, "QQ", QQ, "qq")
    run(hub, {WORK: work, QQ: qq})
    return work, qq


def test_search_local_shape_and_filters(hub_dir):
    indexed_mail(hub_dir)
    out = background.search(hub_dir, mail(hub_dir), "invoice")
    assert out["source"] == "local" and out["errors"] == []
    assert (out["total"], out["next_page"]) == (2, None)
    assert subjects(out) == ["Receipt", "Your invoice"]  # newest first
    item = out["items"][0]
    assert set(item) == {"id", "mailbox", "from", "from_name", "subject", "date", "status",
                         "attachments", "moved_to"}
    assert (item["mailbox"], item["from"], item["status"]) == ("QQ", "shop@store.example", "old")
    assert item["moved_to"] is None  # still in INBOX

    def found(text="", **options):
        return subjects(background.search(hub_dir, mail(hub_dir), text, **options))

    assert found("invoice", mailbox="工作") == ["Your invoice"]
    assert found(sender="mom@") == ["周末回家吗"]
    assert found("回家") == ["周末回家吗"]
    assert found(since="2026-10-08") == ["周末回家吗", "Receipt", "Lunch?"]
    assert found("invoice", sender="shop", since="2026-10-10", mailbox="QQ") == ["Receipt"]
    assert found("no such thing") == []


def test_search_local_pages_at_ten(hub_dir):
    fake = FakeServer()
    for i in range(25):
        old_mail(fake, f"report {i:02d}", 30 + i, sender="a@x.example")
    add(hub_dir, fake)
    run(hub_dir, fake)
    pages = [background.search(hub_dir, mail(hub_dir), "report", page=n) for n in (1, 2, 3)]
    assert [len(p["items"]) for p in pages] == [10, 10, 5]
    assert [p["next_page"] for p in pages] == [2, 3, None]
    assert {p["total"] for p in pages} == {25}
    assert subjects(pages[0])[0] == "report 00"  # newest first


def test_search_unknown_mailbox_and_bad_since(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    with pytest.raises(MailError, match="no mailbox named 'nope'"):
        background.search(hub_dir, mail(hub_dir), "x", mailbox="nope")
    with pytest.raises(MailError, match="2026-10-01"):
        background.search(hub_dir, mail(hub_dir), "x", since="yesterday")
    with pytest.raises(MailError):
        background.search(hub_dir, mail(hub_dir), "x", page=0)


def test_search_local_works_without_a_config_but_a_mailbox_name_needs_it(hub_dir):
    fake = FakeServer()
    fake.deliver(mail_from("a@x.example", "findme"), received=NOW - timedelta(days=2))
    add(hub_dir, fake)
    run(hub_dir, fake)
    (mail(hub_dir) / "config.toml").write_text("[broken", encoding="utf-8")
    out = background.search(hub_dir, mail(hub_dir), "findme")
    assert subjects(out) == ["findme"] and out["items"][0]["mailbox"] == WORK
    with pytest.raises(MailError, match="config"):
        background.search(hub_dir, mail(hub_dir), "findme", mailbox="工作")


# -- search on the server --------------------------------------------------------------------


def server_search(hub, servers, text, **options):
    return background.search(hub, mail(hub), text, on_server=True,
                             connect=fake_connect(servers), **options)


def test_server_search_finds_mail_older_than_the_index_and_stores_nothing(hub_dir):
    fake = FakeServer()
    old_mail(fake, "Tax return 2024", 200)
    old_mail(fake, "Holiday photos", 300)
    add(hub_dir, fake)
    run(hub_dir, fake)
    assert background.search(hub_dir, mail(hub_dir), "return")["total"] == 0  # not in the index
    with Store.open(mail(hub_dir)) as store:
        before = store.search("")["total"]
    fake.calls.clear()

    out = server_search(hub_dir, fake, "return")
    assert out["source"] == "server" and out["errors"] == [] and out["capped"] == {}
    assert (out["total"], out["next_page"]) == (1, None)
    (item,) = out["items"]
    assert set(item) == {"id", "mailbox", "uid", "from", "from_name", "subject", "date",
                         "excerpt", "attachments"}
    assert item["id"] is None and item["uid"] == 1
    assert (item["mailbox"], item["from"], item["subject"]) == (
        "工作", "tax@gov.example", "Tax return 2024")
    assert item["excerpt"] == "about Tax return 2024"
    assert item["date"] == (NOW - timedelta(days=200)).isoformat(timespec="seconds")
    # read-only on the server, and nothing is kept
    assert fake.names() == ["search_text", "fetch", "close"]
    assert fake.calls[0] == ("search_text", "return", None)
    assert fake.writes() == [] and fake.closed
    assert fake.flags == {} and len(fake.inbox) == 2
    with Store.open(mail(hub_dir)) as store:
        assert store.search("")["total"] == before


def test_server_search_passes_since_and_filters_by_sender(hub_dir):
    fake = FakeServer()
    old_mail(fake, "Tax return 2023", 400)
    old_mail(fake, "Tax return 2024", 200)
    old_mail(fake, "Tax return copy", 100, sender="accountant@firm.example")
    add(hub_dir, fake)
    fake.calls.clear()
    out = server_search(hub_dir, fake, "return", since="2026-01-01")
    assert fake.calls[0] == ("search_text", "return", date(2026, 1, 1))
    assert subjects(out) == ["Tax return copy", "Tax return 2024"]  # newest first
    out = server_search(hub_dir, fake, "return", sender="accountant")
    assert subjects(out) == ["Tax return copy"]
    assert out["total"] == 1


def test_server_search_needs_text(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    with pytest.raises(MailError, match="text"):
        server_search(hub_dir, fake, "  ")


def test_server_search_one_mailbox_failing_does_not_stop_the_others(hub_dir):
    work, qq = two_boxes(hub_dir)
    old_mail(work, "Tax return 2024", 200)
    old_mail(qq, "Tax return QQ", 150, to=QQ)
    qq.fail_login = "imap-login"
    out = server_search(hub_dir, {WORK: work, QQ: qq}, "return")
    assert subjects(out) == ["Tax return 2024"]
    assert [e["mailbox"] for e in out["errors"]] == ["QQ"]
    assert "refused the login" in out["errors"][0]["error"]
    assert PASSWORD not in json.dumps(out)
    assert qq.writes() == [] and work.writes() == []
    assert qq.closed  # closed although the login failed


def test_server_search_a_hit_with_an_extreme_date_is_listed_with_no_date(hub_dir):
    """`Date: ... 9999 ... -0500` is a valid date that cannot be written in UTC: the hit stays
    in the answer (last, with `date: null`) and the mailbox is not an error."""
    fake = FakeServer()
    old_mail(fake, "Tax return 2024", 200)
    year9999 = make_raw("Tax return forever", "spam@x.example", WORK, "about Tax return",
                        date="Fri, 31 Dec 9999 23:59:59 -0500")
    fake.deliver(year9999, received=NOW - timedelta(days=1))  # the newest UID
    add(hub_dir, fake)
    assert parse(year9999).date == "9999-12-31T23:59:59-05:00"  # what the test is about
    out = server_search(hub_dir, fake, "return")
    assert out["errors"] == [] and out["total"] == 2
    assert subjects(out) == ["Tax return 2024", "Tax return forever"]
    assert [item["date"] for item in out["items"]][1] is None
    assert out["items"][0]["date"] == (NOW - timedelta(days=200)).isoformat(timespec="seconds")
    json.dumps(out, ensure_ascii=False)


@pytest.mark.parametrize("value, expected", [
    ("9999-12-31T23:59:59-05:00", None),  # past the last UTC moment
    ("0001-01-01T00:00:00+05:00", None),  # before the first
    ("2026-10-14T09:00:00+08:00", "2026-10-14T01:00:00+00:00"),
    ("2026-10-14T09:00:00", "2026-10-14T09:00:00+00:00"),
    ("not a date", None), ("", None), (None, None),
])
def test_utc_iso_never_raises(value, expected):
    assert background._utc_iso(value) == expected


def test_server_search_one_message_that_cannot_be_read_does_not_fail_the_mailbox(
        hub_dir, monkeypatch):
    fake = FakeServer()
    old_mail(fake, "Tax return fine", 200)
    old_mail(fake, "Tax return broken", 100)
    add(hub_dir, fake)
    real = background.parse

    def parse_or_fail(raw):
        if b"broken" in raw:
            raise RecursionError("a message nothing can be read from")
        return real(raw)

    monkeypatch.setattr(background, "parse", parse_or_fail)
    out = server_search(hub_dir, fake, "return")
    assert out["errors"] == []
    assert sorted(item["uid"] for item in out["items"]) == [1, 2]
    broken = next(item for item in out["items"] if item["uid"] == 2)
    assert (broken["subject"], broken["from"], broken["date"], broken["excerpt"]) == (
        background.UNREADABLE, "", None, "")
    assert next(i for i in out["items"] if i["uid"] == 1)["subject"] == "Tax return fine"


def test_server_search_error_text_never_holds_the_password(hub_dir):
    fake = FakeServer()
    old_mail(fake, "Tax return", 200)
    add(hub_dir, fake)
    fake.error = ServerError(f"NO search failed for the login {PASSWORD}")
    out = server_search(hub_dir, fake, "return")
    assert out["items"] == [] and out["total"] == 0
    assert PASSWORD not in json.dumps(out)
    assert "[password]" in out["errors"][0]["error"]


def test_server_search_a_missing_password_is_an_error_entry(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    accounts.secret_path(hub_dir, WORK).unlink()
    logins = len(fake.logins)
    out = server_search(hub_dir, fake, "x")
    assert out["items"] == [] and "no password stored" in out["errors"][0]["error"]
    assert len(fake.logins) == logins  # it never connected


def test_server_search_one_mailbox_by_name(hub_dir):
    work, qq = two_boxes(hub_dir)
    old_mail(work, "Tax return 2024", 200)
    old_mail(qq, "Tax return QQ", 150, to=QQ)
    out = server_search(hub_dir, {WORK: work, QQ: qq}, "return", mailbox="QQ")
    assert subjects(out) == ["Tax return QQ"]
    assert "search_text" in qq.names() and "search_text" not in work.names()
    with pytest.raises(MailError, match="no mailbox named 'nope'"):
        server_search(hub_dir, {WORK: work, QQ: qq}, "return", mailbox="nope")


def test_server_search_pages_all_mailboxes_together_at_ten(hub_dir):
    work, qq = two_boxes(hub_dir)
    for i in range(7):
        old_mail(work, f"report w{i}", 100 + 10 * i)
    for i in range(8):
        old_mail(qq, f"report q{i}", 105 + 10 * i, to=QQ)
    first = server_search(hub_dir, {WORK: work, QQ: qq}, "report")
    second = server_search(hub_dir, {WORK: work, QQ: qq}, "report", page=2)
    assert (first["total"], len(first["items"]), first["next_page"]) == (15, 10, 2)
    assert (second["total"], len(second["items"]), second["next_page"]) == (15, 5, None)
    dates = [item["date"] for item in first["items"] + second["items"]]
    assert dates == sorted(dates, reverse=True)
    assert {item["mailbox"] for item in first["items"]} == {"工作", "QQ"}


def test_server_search_fetches_only_so_many_hits_and_says_so(hub_dir, monkeypatch):
    monkeypatch.setattr(background, "SERVER_HITS", 3)
    fake = FakeServer()
    for i in range(5):  # arrival order: the later the UID, the newer the mail
        old_mail(fake, f"report {i}", 105 - i)
    add(hub_dir, fake)
    fake.calls.clear()
    out = server_search(hub_dir, fake, "report")
    assert out["total"] == 3 and out["capped"] == {"工作": 5}
    assert subjects(out) == ["report 4", "report 3", "report 2"]  # the newest
    fetched = [uid for c in fake.calls if c[0] == "fetch" for uid in c[1]]
    assert sorted(fetched) == [3, 4, 5]


def test_server_search_error_when_the_config_is_broken(hub_dir):
    (mail(hub_dir) / "config.toml").write_text("[broken", encoding="utf-8")
    with pytest.raises(MailError, match="config"):
        server_search(hub_dir, FakeServer(), "x")


# -- recent-pushed ---------------------------------------------------------------------------


def push_mail(hub, count):
    fake = FakeServer()
    add(hub, fake)
    for i in range(count):
        fake.deliver(mail_from("boss@corp.example", f"push {i:02d}"))
    run(hub, fake)
    ids = pending_ids(hub)
    for n, mid in enumerate(ids):
        judged(hub, {mid: "important"}, minutes=n)
    return ids


def test_recent_pushed_newest_first_default_five(hub_dir):
    push_mail(hub_dir, 8)
    out = background.recent_pushed(mail(hub_dir))
    assert subjects(out) == [f"push {i:02d}" for i in (7, 6, 5, 4, 3)]
    assert (out["total"], out["limit"]) == (8, 5)
    item = out["items"][0]
    assert set(item) == {"id", "mailbox", "from", "from_name", "subject", "date", "status",
                         "attachments", "moved_to", "pushed_at"}
    assert (item["mailbox"], item["status"], item["pushed_at"]) == (
        "工作", "important", "2026-10-14T04:07:00+00:00")
    assert len(background.recent_pushed(mail(hub_dir), limit=2)["items"]) == 2


def test_recent_pushed_limit_is_capped(hub_dir):
    push_mail(hub_dir, 25)
    out = background.recent_pushed(mail(hub_dir), limit=500)
    assert len(out["items"]) == background.PUSHED_MAX == 20
    assert (out["limit"], out["total"]) == (20, 25)
    with pytest.raises(MailError):
        background.recent_pushed(mail(hub_dir), limit=0)


def test_recent_pushed_with_no_pushes(hub_dir):
    assert background.recent_pushed(mail(hub_dir)) == {"items": [], "total": 0, "limit": 5}


# -- problems --------------------------------------------------------------------------------


def flap(hub, servers, box, cycles, start=10):
    """`box` fails and recovers `cycles` times, a poll every 5 minutes; returns the next minute."""
    minute = start
    for _ in range(cycles):
        box.fail_login = "imap-login"
        run(hub, servers, minutes=minute)
        box.fail_login = None
        run(hub, servers, minutes=minute + 5)
        minute += 10
    return minute


def test_problems_shape_and_a_report_marks_them(hub_dir):
    work, qq = two_boxes(hub_dir)
    servers = {WORK: work, QQ: qq}
    qq.fail_login = "imap-login"
    run(hub_dir, servers, minutes=1)
    out = background.problems(mail(hub_dir))
    assert set(out) == {"problems", "total", "next_page"}
    assert (out["total"], out["next_page"]) == (1, None)
    (p,) = out["problems"]
    assert set(p) == {"ids", "mailbox", "address", "kind", "state", "message", "first_seen",
                      "last_seen", "resolved_at", "reported_at", "count"}
    assert (p["mailbox"], p["address"], p["kind"], p["state"], p["count"]) == (
        "QQ", QQ, "login", "open", 1)
    assert "refused the login" in p["message"]
    assert (p["resolved_at"], p["reported_at"]) == (None, None)
    assert p["first_seen"] == p["last_seen"] == "2026-10-14T04:01:00+00:00"

    marked = background.problems(mail(hub_dir), mark_reported=p["ids"], now=t(2))
    assert marked == {"marked": 1, "unreported_left": 0}
    assert background.problems(mail(hub_dir))["problems"] == []


def test_problems_recoveries_of_one_mailbox_and_kind_are_one_item(hub_dir):
    work, qq = two_boxes(hub_dir)
    servers = {WORK: work, QQ: qq}
    flap(hub_dir, servers, qq, cycles=3)
    out = background.problems(mail(hub_dir))
    assert out["total"] == 1
    (p,) = out["problems"]
    assert (p["state"], p["mailbox"], p["kind"], p["count"]) == ("recovered", "QQ", "login", 3)
    assert len(p["ids"]) == 3 and p["ids"] == sorted(p["ids"])
    assert p["first_seen"] == "2026-10-14T04:10:00+00:00"
    assert p["last_seen"] == "2026-10-14T04:30:00+00:00"
    assert p["resolved_at"] == "2026-10-14T04:35:00+00:00"

    assert background.problems(mail(hub_dir), mark_reported=p["ids"], now=t(60)) == {
        "marked": 3, "unreported_left": 0}
    assert background.problems(mail(hub_dir))["problems"] == []


def test_problems_keep_other_kinds_and_mailboxes_apart(hub_dir):
    work, qq = two_boxes(hub_dir)
    servers = {WORK: work, QQ: qq}
    flap(hub_dir, servers, qq, cycles=2)
    work.fail_login = "connect"
    qq.fail_login = "imap-login"
    run(hub_dir, servers, minutes=60)
    out = background.problems(mail(hub_dir))
    seen = [(p["mailbox"], p["kind"], p["state"], p["count"]) for p in out["problems"]]
    assert seen == [("QQ", "login", "recovered", 2), ("工作", "connect", "open", 1),
                    ("QQ", "login", "open", 1)]


def test_problems_unknown_ids_are_ignored(hub_dir):
    work, qq = two_boxes(hub_dir)
    qq.fail_login = "imap-login"
    run(hub_dir, {WORK: work, QQ: qq}, minutes=1)
    out = background.problems(mail(hub_dir), mark_reported=[999], now=t(2))
    assert out == {"marked": 0, "unreported_left": 1}


@pytest.mark.parametrize("how", ["broken", "missing"])
def test_problems_work_without_a_usable_config(hub_dir, how):
    fake = FakeServer()
    add(hub_dir, fake)
    path = mail(hub_dir) / "config.toml"
    if how == "broken":
        path.write_text("[broken", encoding="utf-8")
    else:
        path.unlink()
    run(hub_dir, fake)
    out = background.problems(mail(hub_dir))
    (p,) = out["problems"]
    assert (p["kind"], p["address"], p["mailbox"]) == ("config", "config", None)
    assert background.problems(mail(hub_dir), mark_reported=p["ids"], now=t(1))["marked"] == 1
    assert background.problems(mail(hub_dir))["problems"] == []


def test_problems_page_at_ten(hub_dir):
    with Store.open(mail(hub_dir)) as store:
        for i in range(13):
            store.problem(f"box{i}@x.example", "login", f"problem {i}", t(i))
    one = background.problems(mail(hub_dir))
    two = background.problems(mail(hub_dir), page=2)
    assert (len(one["problems"]), one["total"], one["next_page"]) == (10, 13, 2)
    assert (len(two["problems"]), two["next_page"]) == (3, None)
    with pytest.raises(MailError):
        background.problems(mail(hub_dir), page=0)


def test_problems_inside_other_listings_are_capped_with_a_total(hub_dir):
    with Store.open(mail(hub_dir)) as store:
        for i in range(13):
            store.problem(f"box{i}@x.example", "login", f"problem {i}", t(i))
    out = background.pending(mail(hub_dir))
    assert len(out["problems"]) == 10 and out["problems_total"] == 13


# -- digest ----------------------------------------------------------------------------------


def digest_scene(hub):
    """工作 (instant) with three judged mails, QQ (digest delivery) with two routed ones."""
    work, qq = two_boxes(hub)
    set_mailbox(hub, "QQ", "delivery", "digest")
    for i in range(3):
        work.deliver(mail_from("boss@corp.example", f"w{i}"))
    qq.deliver(mail_from("ads@shop.example", "q0", to=QQ))
    qq.deliver(mail_from("ads@shop.example", "q1", to=QQ))
    run(hub, {WORK: work, QQ: qq}, minutes=1)
    a, b, c = pending_ids(hub)
    judged(hub, {a: "important", b: "digest", c: "digest"}, minutes=2)
    return work, qq


def test_digest_items_groups_by_mailbox(hub_dir):
    digest_scene(hub_dir)
    out = background.digest_items(mail(hub_dir), now=t(3))
    assert set(out) == {"since", "up_to", "groups", "total", "next_page", "pushed_count",
                        "mailbox_totals", "problems", "problems_total", "stale", "last_poll",
                        "rules", "config_error", *WAKE_KEYS}
    assert out["since"] is None and out["pushed_count"] == 1
    assert (out["total"], out["next_page"], out["problems"], out["config_error"]) == (
        4, None, [], None)
    assert out["up_to"] >= 4
    assert [(g["mailbox"], g["count"], subjects(g)) for g in out["groups"]] == [
        ("工作", 2, ["w1", "w2"]), ("QQ", 2, ["q0", "q1"])]
    item = out["groups"][0]["items"][0]
    assert set(item) == {"id", "mailbox", "from", "from_name", "addressed", "subject", "date",
                         "excerpt", "attachments", "bulk"}
    settings = config.load(mail(hub_dir))
    assert out["rules"]["QQ"] == config.effective(settings, "QQ")
    assert out["last_poll"] == "2026-10-14T04:01:00+00:00"
    assert out["stale"] is False


def test_digest_items_excerpt_is_short(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    set_mailbox(hub_dir, "工作", "delivery", "digest")
    fake.deliver(mail_from("ads@shop.example", "long", body="y" * 3000))
    run(hub_dir, fake)
    out = background.digest_items(mail(hub_dir), now=t(1))
    assert len(out["groups"][0]["items"][0]["excerpt"]) == background.DIGEST_EXCERPT_CHARS == 500


def test_digest_items_pages_with_a_fixed_up_to_and_done_marks_only_what_was_listed(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    set_mailbox(hub_dir, "工作", "delivery", "digest")
    for i in range(25):
        fake.deliver(mail_from("ads@shop.example", f"d{i:02d}"))
    run(hub_dir, fake, minutes=1)

    one = background.digest_items(mail(hub_dir), now=t(2))
    up_to = one["up_to"]
    assert (one["total"], one["next_page"]) == (25, 2)
    assert [len(g["items"]) for g in one["groups"]] == [10]
    fake.deliver(mail_from("ads@shop.example", "late"))
    run(hub_dir, fake, minutes=3)  # routed to the digest after the listing began
    two = background.digest_items(mail(hub_dir), page=2, up_to=up_to, now=t(4))
    three = background.digest_items(mail(hub_dir), page=3, up_to=up_to, now=t(4))
    assert (two["up_to"], two["total"], two["next_page"]) == (up_to, 25, 3)
    assert (three["total"], three["next_page"], len(three["groups"][0]["items"])) == (25, None, 5)
    seen = [s for out in (one, two, three) for g in out["groups"] for s in subjects(g)]
    assert seen == [f"d{i:02d}" for i in range(25)]

    done = background.digest_done(mail(hub_dir), up_to, now=t(5))
    assert done == {"digested": 25, "last_digest": "2026-10-14T04:05:00+00:00"}
    nxt = background.digest_items(mail(hub_dir), now=t(6))
    assert (nxt["since"], nxt["total"]) == ("2026-10-14T04:05:00+00:00", 1)
    assert subjects(nxt["groups"][0]) == ["late"]


def test_digest_items_total_every_mailbox_on_every_page(hub_dir):
    """Pages run by address, so a mailbox can sit wholly on a late page: every page says how
    much of the listing each mailbox holds."""
    work, qq = two_boxes(hub_dir)
    servers = {WORK: work, QQ: qq}
    set_mailbox(hub_dir, "工作", "delivery", "digest")
    set_mailbox(hub_dir, "QQ", "delivery", "digest")
    for i in range(15):
        work.deliver(mail_from("ads@shop.example", f"w{i:02d}"))
    for i in range(12):
        qq.deliver(mail_from("ads@shop.example", f"q{i:02d}", to=QQ))
    run(hub_dir, servers, minutes=1)

    totals = [{"mailbox": "工作", "total": 15}, {"mailbox": "QQ", "total": 12}]
    one = background.digest_items(mail(hub_dir), now=t(2))
    assert one["mailbox_totals"] == totals
    assert [g["mailbox"] for g in one["groups"]] == ["工作"]  # QQ is not on page 1
    qq.deliver(mail_from("ads@shop.example", "late", to=QQ))
    run(hub_dir, servers, minutes=3)  # outside the listing
    three = background.digest_items(mail(hub_dir), page=3, up_to=one["up_to"], now=t(4))
    assert three["mailbox_totals"] == totals
    assert background.digest_items(mail(hub_dir), now=t(5))["mailbox_totals"][1] == {
        "mailbox": "QQ", "total": 13}
    empty = background.digest_items(mail(hub_dir), up_to=0, now=t(5))
    assert empty["mailbox_totals"] == []


def test_digest_done_with_nothing_listed_still_records_the_digest(hub_dir):
    out = background.digest_items(mail(hub_dir), now=t(1))
    assert (out["groups"], out["total"], out["up_to"]) == ([], 0, 0)
    assert background.digest_done(mail(hub_dir), 0, now=t(2)) == {
        "digested": 0, "last_digest": "2026-10-14T04:02:00+00:00"}
    assert background.digest_items(mail(hub_dir), now=t(3))["since"] == "2026-10-14T04:02:00+00:00"


def test_digest_items_refuses_a_negative_up_to_and_page(hub_dir):
    with pytest.raises(MailError):
        background.digest_items(mail(hub_dir), up_to=-1)
    with pytest.raises(MailError):
        background.digest_items(mail(hub_dir), page=0)
    with pytest.raises(MailError):
        background.digest_done(mail(hub_dir), -1, now=t(1))


def test_digest_items_counts_pushes_since_the_last_digest(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    for i in range(2):
        fake.deliver(mail_from("boss@corp.example", f"p{i}"))
    run(hub_dir, fake, minutes=1)
    first, second = pending_ids(hub_dir)
    judged(hub_dir, {first: "important"}, minutes=2)
    assert background.digest_items(mail(hub_dir), now=t(3))["pushed_count"] == 1
    background.digest_done(mail(hub_dir), 0, now=t(10))
    assert background.digest_items(mail(hub_dir), now=t(11))["pushed_count"] == 0
    judged(hub_dir, {second: "important"}, minutes=12)
    out = background.digest_items(mail(hub_dir), now=t(13))
    assert (out["pushed_count"], out["since"]) == (1, "2026-10-14T04:10:00+00:00")


def test_digest_items_lists_every_open_problem_and_the_recoveries_once(hub_dir):
    work, qq = two_boxes(hub_dir)
    servers = {WORK: work, QQ: qq}
    qq.fail_login = "imap-login"
    run(hub_dir, servers, minutes=1)
    with Store.open(mail(hub_dir)) as store:
        store.mark_reported([p["id"] for p in store.problems(unreported_only=True)], t(2))
    # reported before, still open: not in `pending`, but the digest lists it again
    assert background.pending(mail(hub_dir))["problems"] == []
    out = background.digest_items(mail(hub_dir), now=t(3))
    (p,) = out["problems"]
    assert (p["mailbox"], p["kind"], p["state"]) == ("QQ", "login", "open")
    assert p["reported_at"] == "2026-10-14T04:02:00+00:00"
    flap(hub_dir, servers, work, cycles=2, start=20)
    out = background.digest_items(mail(hub_dir), now=t(50))
    assert [(p["mailbox"], p["state"], p["count"]) for p in out["problems"]] == [
        ("QQ", "open", 1), ("工作", "recovered", 2)]
    assert out["problems_total"] == 2


def test_digest_items_without_a_config_still_answers(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    set_mailbox(hub_dir, "工作", "delivery", "digest")
    fake.deliver(mail_from("ads@shop.example", "ad"))
    run(hub_dir, fake)
    (mail(hub_dir) / "config.toml").unlink()
    run(hub_dir, fake, minutes=5)
    out = background.digest_items(mail(hub_dir), now=t(6))
    assert out["rules"] == {} and "config.toml" in out["config_error"]
    assert [p["kind"] for p in out["problems"]] == ["config"]
    assert [(g["mailbox"], subjects(g)) for g in out["groups"]] == [(WORK, ["ad"])]


def test_digest_items_say_when_the_poll_stopped(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    for n in range(5):
        run(hub_dir, fake, minutes=5 * n)
    assert background.digest_items(mail(hub_dir), now=t(20 + 15))["stale"] is False
    assert background.digest_items(mail(hub_dir), now=t(20 + 21))["stale"] is True


def test_the_digests_own_polls_never_make_a_dead_check_look_alive(hub_dir):
    """The poll task runs every 5 minutes, then dies; only the daily digest polls (`bg poll`)
    for eleven days. The check stays stale, with the time of its last real run."""
    fake = FakeServer()
    add(hub_dir, fake)
    for n in range(5):
        run(hub_dir, fake, minutes=5 * n)
    for day in range(1, 12):
        fake.deliver(mail_from("ads@shop.example", f"day {day}"))
        assert run(hub_dir, fake, minutes=day * 1440, background=False)["pending"] >= 1
    now = t(11 * 1440 + 1)
    out = background.status(mail(hub_dir), now=now)
    assert (out["stale"], out["last_poll"], out["threshold_minutes"]) == (
        True, "2026-10-14T04:20:00+00:00", 20)
    digest = background.digest_items(mail(hub_dir), now=now)
    assert (digest["stale"], digest["last_poll"]) == (True, "2026-10-14T04:20:00+00:00")


def test_the_cli_bg_poll_leaves_the_status_stale(hub_dir, monkeypatch, capsys):
    fake = FakeServer()
    add(hub_dir, fake)
    monkeypatch.setattr(server, "connect", fake_connect(fake))
    argv = ["--mail-dir", str(mail(hub_dir)), "--hub", str(hub_dir)]
    assert cli.main([*argv, "bg", "poll"]) == 0
    capsys.readouterr()
    assert cli.main([*argv, "bg", "status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["last_poll"], out["stale"]) == (None, True)


# -- status ----------------------------------------------------------------------------------


def record_polls(hub, minutes):
    with Store.open(mail(hub)) as store:
        for minute in minutes:
            store.record_poll(t(minute))


def test_status_stale_after_two_median_intervals_and_ten_minutes(hub_dir):
    record_polls(hub_dir, [0, 5, 10, 15, 20])
    fresh = background.status(mail(hub_dir), now=t(20 + 15))
    assert fresh["stale"] is False and fresh["threshold_minutes"] == 20
    assert fresh["last_poll"] == "2026-10-14T04:20:00+00:00"
    edge = background.status(mail(hub_dir), now=t(20 + 20))
    assert edge["stale"] is False  # stale means longer than the threshold
    late = background.status(mail(hub_dir), now=t(20 + 21))
    assert late["stale"] is True and late["threshold_minutes"] == 20


def test_status_with_fewer_than_two_polls_assumes_five_minutes(hub_dir):
    record_polls(hub_dir, [0])
    assert background.status(mail(hub_dir), now=t(19))["stale"] is False
    out = background.status(mail(hub_dir), now=t(21))
    assert (out["stale"], out["threshold_minutes"]) == (True, 20)


def test_status_with_no_poll_at_all_is_stale(hub_dir):
    out = background.status(mail(hub_dir), now=t(1))
    assert (out["last_poll"], out["stale"], out["threshold_minutes"]) == (None, True, 20)
    assert out["mailboxes"] == []


def test_status_threshold_follows_a_slow_cadence(hub_dir):
    record_polls(hub_dir, [0, 1440, 2880])
    last = 2880
    assert background.status(mail(hub_dir), now=t(last + 2889))["stale"] is False
    out = background.status(mail(hub_dir), now=t(last + 2891))
    assert (out["stale"], out["threshold_minutes"]) == (True, 2890)


def test_status_uses_the_median_so_an_extra_poll_does_not_shorten_it(hub_dir):
    # the cron every 5 minutes, and the task run by hand (cron exec) 3 minutes after one of them
    record_polls(hub_dir, [0, 5, 10, 13, 15, 20, 25, 30])
    assert background.status(mail(hub_dir), now=t(30))["threshold_minutes"] == 20


def test_status_mailboxes(hub_dir):
    work, qq = two_boxes(hub_dir)
    servers = {WORK: work, QQ: qq}
    run(hub_dir, servers, minutes=0)
    qq.fail_login = "imap-login"
    run(hub_dir, servers, minutes=5)
    out = background.status(mail(hub_dir), now=t(6))
    assert set(out) == {"last_poll", "stale", "threshold_minutes", "mailboxes", "config_error",
                        *WAKE_KEYS}
    assert out["config_error"] is None and out["stale"] is False
    ok, bad = out["mailboxes"]
    assert (ok["name"], ok["address"], ok["last_ok"], ok["problem"]) == (
        "工作", WORK, "2026-10-14T04:05:00+00:00", None)
    assert (bad["name"], bad["address"], bad["last_ok"]) == (
        "QQ", QQ, "2026-10-14T04:00:00+00:00")
    assert bad["problem"]["kind"] == "login"
    assert "refused the login" in bad["problem"]["message"]
    assert bad["problem"]["first_seen"] == "2026-10-14T04:05:00+00:00"
    qq.fail_login = None
    run(hub_dir, servers, minutes=10)
    assert background.status(mail(hub_dir), now=t(11))["mailboxes"][1]["problem"] is None


def test_status_and_the_digest_say_whether_the_wakes_reach_triage(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    fake.deliver(mail_from("boss@corp.example", "waits"))
    for minutes in range(0, 70, 5):  # the check runs, every wake fails
        run(hub_dir, fake, minutes=minutes, wake=lambda: "cc-connect: connection refused")
    expected = {
        "last_wake_at": None, "wake_error": "cc-connect: connection refused",
        "wake_error_since": "2026-10-14T04:00:00+00:00", "unacknowledged_wakes": 0,
        "oldest_pending_at": "2026-10-14T04:00:00+00:00", "oldest_pending_minutes": 66}
    out = background.status(mail(hub_dir), now=t(66))
    assert out["stale"] is False and {k: out[k] for k in WAKE_KEYS} == expected
    digest = background.digest_items(mail(hub_dir), now=t(66))
    assert {k: digest[k] for k in WAKE_KEYS} == expected


def test_status_counts_the_wakes_triage_did_not_answer(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    fake.deliver(mail_from("boss@corp.example", "waits"))
    for minutes in range(0, 35, 5):
        run(hub_dir, fake, minutes=minutes, wake=lambda: None)
    out = background.status(mail(hub_dir), now=t(31))
    assert (out["wake_error"], out["last_wake_at"], out["unacknowledged_wakes"]) == (
        None, "2026-10-14T04:30:00+00:00", 3)
    (mid,) = pending_ids(hub_dir)
    judged(hub_dir, {mid: "important"}, minutes=32)
    out = background.status(mail(hub_dir), now=t(33))
    assert (out["unacknowledged_wakes"], out["oldest_pending_minutes"]) == (0, None)


def test_status_with_a_broken_config(hub_dir):
    fake = FakeServer()
    add(hub_dir, fake)
    run(hub_dir, fake)
    (mail(hub_dir) / "config.toml").write_text("[broken", encoding="utf-8")
    out = background.status(mail(hub_dir), now=t(1))
    assert out["mailboxes"] == [] and "config.toml" in out["config_error"]
    assert out["last_poll"] == "2026-10-14T04:00:00+00:00"


# -- the command line ------------------------------------------------------------------------


@pytest.fixture
def mk(hub_dir, monkeypatch, capsys):
    fake = FakeServer()
    monkeypatch.setattr(server, "connect", fake_connect(fake))
    woken = []
    monkeypatch.setattr(poll, "wake_triage", lambda hub=None: woken.append(hub))

    def run_mk(*args):
        argv = ["--mail-dir", str(mail(hub_dir)), "--hub", str(hub_dir), *args]
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        status = cli.main(argv)
        out, err = capsys.readouterr()
        return status, json.loads(out) if out.strip() else out, err

    run_mk.fake = fake
    run_mk.woken = woken
    return run_mk


def test_cli_bg_commands_print_json(hub_dir, mk):
    add(hub_dir, mk.fake)
    mk.fake.deliver(mail_from("boss@corp.example", "needs you", body="please reply"))
    mk.fake.deliver(mail_from("ads@shop.example", "sale"))
    status, out, err = mk("bg", "poll")
    assert (status, err) == (0, "") and out["pending"] == 2 and out["woke"] is False
    assert mk.woken == []

    status, out, err = mk("bg", "pending")
    assert (status, err, out["total"]) == (0, "", 2)
    first, second = (item["id"] for item in out["items"])
    status, out, _ = mk("bg", "pending", "--page", "2")
    assert (status, out["items"]) == (0, [])

    status, out, _ = mk("bg", "read", str(first))
    assert (status, out["subject"], out["text"].strip()) == (0, "needs you", "please reply")

    status, out, _ = mk("bg", "judge", f"{first}=important", f"{second}=digest")
    assert (status, out) == (0, {"judged": 2, "important": [first], "digest": [second],
                                 "already_judged": [], "pending_left": 0})
    status, out, _ = mk("bg", "recent-pushed")
    assert (status, subjects(out)) == (0, ["needs you"])
    status, out, _ = mk("bg", "recent-pushed", "--limit", "1")
    assert (status, out["limit"]) == (0, 1)

    status, out, _ = mk("bg", "search", "needs", "--mailbox", "工作", "--from", "boss",
                        "--since", "2026-10-01")
    assert (status, out["total"]) == (0, 1)
    status, out, _ = mk("bg", "search", "--from", "ads")
    assert (status, subjects(out)) == (0, ["sale"])

    status, out, _ = mk("bg", "problems")
    assert (status, out["problems"]) == (0, [])
    status, out, _ = mk("bg", "status")
    assert status == 0 and out["mailboxes"][0]["name"] == "工作"
    assert (out["last_poll"], out["stale"]) == (None, True)  # `bg poll` is no run of the task

    status, out, _ = mk("bg", "digest-items")
    assert status == 0 and out["total"] == 1 and out["pushed_count"] == 1
    up_to = out["up_to"]
    status, out, _ = mk("bg", "digest-items", "--page", "1", "--up-to", str(up_to))
    assert (status, out["up_to"], out["total"]) == (0, up_to, 1)
    status, out, _ = mk("bg", "digest-done", str(up_to))
    assert (status, out["digested"]) == (0, 1)


def test_cli_bg_search_on_the_server(hub_dir, mk):
    old_mail(mk.fake, "Tax return 2024", 200)
    add(hub_dir, mk.fake)
    status, out, err = mk("bg", "search", "return", "--server", "--since", "2026-01-01")
    assert (status, err) == (0, "")
    assert out["source"] == "server" and subjects(out) == ["Tax return 2024"]
    assert mk.fake.writes() == []


def test_cli_bg_problems_mark_reported(hub_dir, mk):
    add(hub_dir, mk.fake)
    mk.fake.fail_login = "imap-login"
    mk("bg", "poll")
    status, out, _ = mk("bg", "problems")
    (p,) = out["problems"]
    status, out, _ = mk("bg", "problems", "--mark-reported", str(p["ids"][0]), "999")
    assert (status, out) == (0, {"marked": 1, "unreported_left": 0})


def test_cli_bg_problems_without_a_config(hub_dir, mk):
    add(hub_dir, mk.fake)
    (mail(hub_dir) / "config.toml").unlink()
    mk("bg", "poll")
    status, out, err = mk("bg", "problems")
    assert (status, err) == (0, "") and out["problems"][0]["kind"] == "config"
    status, out, _ = mk("bg", "problems", "--mark-reported", str(out["problems"][0]["ids"][0]))
    assert (status, out["marked"]) == (0, 1)


def test_cli_poll_wakes_but_bg_poll_cannot(hub_dir, mk):
    add(hub_dir, mk.fake)
    mk.fake.deliver(mail_from("boss@corp.example"))
    status, out, err = mk("bg", "poll", "--wake")
    assert status == 1 and out == "" and err.startswith("usage:") and "--wake" in err
    assert "mk: unrecognized arguments: --wake" in err
    assert mk.woken == []

    status, out, err = mk("poll", "--wake")
    assert (status, err) == (0, "")
    assert (out["pending"], out["woke"], out["wake_error"]) == (1, True, None)
    assert mk.woken == [hub_dir]


def test_cli_poll_without_wake_does_not_wake(hub_dir, mk):
    add(hub_dir, mk.fake)
    mk.fake.deliver(mail_from("boss@corp.example"))
    status, out, _ = mk("poll")
    assert status == 0 and out["pending"] == 1 and out["woke"] is False
    assert mk.woken == []


def test_cli_bg_group_is_exactly_the_read_only_commands(hub_dir):
    """A new `bg` command (one the restricted tasks may run) must be a deliberate change."""
    parser = cli.build_parser()
    top = next(a for a in parser._actions if a.dest == "command")
    bg = top.choices["bg"]
    actions = next(a for a in bg._actions if a.dest == "action")
    assert sorted(actions.choices) == [
        "attachment", "digest-done", "digest-items", "judge", "pending", "poll", "problems",
        "read", "recent-pushed", "search", "status"]
    for name, sub in actions.choices.items():
        options = {o for a in sub._actions for o in a.option_strings}
        assert "--wake" not in options, name
        assert not options & {"--password-stdin", "--send", "--delete", "--move"}, name


@pytest.mark.parametrize("argv, message", [
    (["bg", "pending", "--page", "0"], "at least 1"),
    (["bg", "pending", "--page", "x"], "not a number"),
    (["bg", "pending", "--page", "1_0"], "not a number"),
    (["bg", "read", "abc"], "not a number"),
    (["bg", "judge"], "required"),
    (["bg", "judge", "3"], "ID=DECISION"),
    (["bg", "judge", "3=maybe"], "important, digest"),
    (["bg", "digest-done"], "required"),
    (["bg", "digest-done", "-1"], "not a number"),
    (["bg", "digest-done", "99999999999999999999"], "too large"),
    (["bg", "digest-done", "x"], "not a number"),
    (["bg", "digest-items", "--up-to", "x"], "not a number"),
    (["bg", "recent-pushed", "--limit", "0"], "at least 1"),
    (["bg", "search", "x", "--since", "soon"], "2026-10-01"),
    (["bg", "search", "x", "--mailbox", "nope"], "no mailbox named"),
    (["bg", "search", "", "--server"], "text"),
    (["bg", "problems", "--mark-reported"], "expected at least one"),
    (["bg", "read", "9"], "no message with id 9"),
    (["bg", "read", "99999999999999999999"], "too large"),
    (["bg", "judge", "99999999999999999999=digest"], "no message with id"),
    (["bg", "problems", "--mark-reported", "0"], "at least 1"),
])
def test_cli_bg_usage_errors_are_mk_errors(hub_dir, mk, argv, message):
    add(hub_dir, mk.fake)
    status, out, err = mk(*argv)
    assert status == 1 and out == ""
    assert "mk: " in err and message in err
    assert "Traceback" not in err


def test_cli_prints_a_failing_index_as_an_mk_error(hub_dir, mk, monkeypatch):
    def broken(*_args, **_kwargs):
        raise sqlite3.DatabaseError("disk image is malformed")

    monkeypatch.setattr(Store, "open", classmethod(broken))
    status, out, err = mk("bg", "pending")
    assert status == 1 and out == ""
    assert err.startswith("mk: ") and "disk image is malformed" in err


def test_cli_never_prints_the_password(hub_dir, mk):
    add(hub_dir, mk.fake)
    mk.fake.error = ServerError(f"NO login {PASSWORD} failed")
    mk("bg", "poll")
    old_mail(mk.fake, "Tax return", 200)
    mk.fake.error = None
    status, out, err = mk("bg", "search", "return", "--server")
    text = json.dumps(out) + err
    assert PASSWORD not in text
    mk.fake.error = ServerError(f"NO search for {PASSWORD}")
    status, out, err = mk("bg", "search", "return", "--server")
    assert status == 0 and PASSWORD not in json.dumps(out) + err
    for command in (("bg", "problems"), ("bg", "status"), ("bg", "pending")):
        _status, out, err = mk(*command)
        assert PASSWORD not in json.dumps(out) + err, command

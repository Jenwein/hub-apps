import json
import sqlite3
import threading
from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from mailkit.config import MailError
from mailkit.message import Attachment, Parsed
from mailkit.store import Store

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
A = "me@corp.example"
B = "me+shop@qq.example"


def t(minutes: float = 0) -> datetime:
    return NOW + timedelta(minutes=minutes)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def parsed(**changes) -> Parsed:
    fields = {
        "message_id": "<m1@host>", "date": "2026-10-14T09:30:00+08:00",
        "from_addr": "boss@corp.example", "from_name": "Boss", "to": [A],
        "subject": "hello", "text": "see you on Wednesday",
    }
    fields.update(changes)
    return Parsed(**fields)


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "mail")
    yield s
    s.close()


def put(store, uid, *, address=A, uidvalidity=1, status="pending", addressed="to", now=NOW,
        **changes):
    """Add message `uid`; the subject defaults to "message <uid>"."""
    changes.setdefault("subject", f"message {uid}")
    changes.setdefault("message_id", f"<{uid}@host>")
    return store.add(address, uid, uidvalidity, parsed(**changes), status, addressed, now=now)


# -- opening ---------------------------------------------------------------------------------


def test_open_creates_the_state_directory_and_schema(tmp_path):
    mail = tmp_path / "hub" / "mail"  # neither mail/ nor mail/.state/ exists yet
    with Store.open(mail) as s:
        assert s.state(A) is None
    db = mail / ".state" / "mail.db"
    assert db.is_file()
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone() == (1,)
    con.close()


def test_open_in_a_directory_with_chinese_in_its_path(tmp_path):
    mail = tmp_path / "总站 邮件" / "mail"
    with Store.open(mail) as s:
        put(s, 1, subject="面试")
    with Store.open(mail) as s:
        assert s.search("面试")["total"] == 1


def test_open_again_keeps_the_data(tmp_path):
    with Store.open(tmp_path) as s:
        s.set_baseline(A, 7, 40, 12)
        put(s, 1)
    with Store.open(tmp_path) as s:
        assert s.state(A) == {"uidvalidity": 7, "last_uid": 40, "backfill_before": 12}
        assert s.search("message")["total"] == 1


def test_open_refuses_an_index_made_by_a_newer_version(tmp_path):
    (tmp_path / ".state").mkdir()
    con = sqlite3.connect(tmp_path / ".state" / "mail.db")
    con.execute("PRAGMA user_version = 2")
    con.close()
    with pytest.raises(MailError, match="newer"):
        Store.open(tmp_path)


def test_open_reports_a_file_that_is_not_a_database(tmp_path):
    (tmp_path / ".state").mkdir()
    (tmp_path / ".state" / "mail.db").write_bytes(b"this is not sqlite" * 200)
    with pytest.raises(MailError, match="mail.db"):
        Store.open(tmp_path)


def test_two_commands_can_open_and_write_at_the_same_time(tmp_path):
    errors = []
    start = threading.Barrier(2)  # both open a store that does not exist yet

    def work(address):
        try:
            start.wait()
            with Store.open(tmp_path / "mail") as s:
                for uid in range(1, 61):
                    put(s, uid, address=address)
                    s.record_poll(t(uid))
        except Exception as error:  # noqa: BLE001 - the test reports whatever happened
            errors.append(error)

    threads = [threading.Thread(target=work, args=(a,)) for a in (A, B)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    with Store.open(tmp_path / "mail") as s:
        assert s.search("")["total"] == 120


def test_a_locked_index_fails_with_a_mail_error_not_a_traceback(tmp_path):
    Store.open(tmp_path).close()
    other = sqlite3.connect(tmp_path / ".state" / "mail.db", isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        with Store.open(tmp_path, busy_timeout_ms=50) as s, pytest.raises(MailError, match="busy"):
            s.record_poll(NOW)
    finally:
        other.execute("ROLLBACK")
        other.close()


# -- mailbox state ---------------------------------------------------------------------------


def test_baseline_state_and_update(store):
    assert store.state(A) is None
    store.set_baseline(A, 111, 50, None)
    assert store.state(A) == {"uidvalidity": 111, "last_uid": 50, "backfill_before": None}
    store.set_baseline(A, 111, 55, 20)
    assert store.state(A) == {"uidvalidity": 111, "last_uid": 55, "backfill_before": 20}
    store.set_baseline(A, 222, 3, 1)  # UIDVALIDITY changed: the baseline is simply replaced
    assert store.state(A) == {"uidvalidity": 222, "last_uid": 3, "backfill_before": 1}


def test_forget_removes_one_mailbox_only(store):
    for address in (A, B):
        store.set_baseline(address, 1, 5, None)
        put(store, 1, address=address, status="pending")
        store.problem(address, "login", "bad password", NOW)
    [a_message] = [m["id"] for m in store.pending(10, 1)["items"] if m["address"] == A]
    store.judge({a_message: "important"}, NOW)

    store.forget(A)

    assert store.state(A) is None and store.state(B) is not None
    assert [m["address"] for m in store.search("")["items"]] == [B]
    assert [p["address"] for p in store.problems(unreported_only=False)] == [B]
    assert store.pushes() == []


# -- adding messages -------------------------------------------------------------------------


def test_add_returns_ids_and_ignores_duplicates(store):
    first = put(store, 1)
    again = put(store, 1, subject="changed", status="digest")  # same (address, uidvalidity, uid)
    other = put(store, 2)
    assert again == first and other != first
    assert store.search("")["total"] == 2
    kept = store.message(first)
    assert kept["subject"] == "message 1" and kept["status"] == "pending"


def test_add_without_now_uses_the_clock(store):
    before = iso(datetime.now(UTC))
    mid = store.add(A, 1, 1, parsed(date=None), "pending", "to")
    assert before <= store.message(mid)["added_at"] <= iso(datetime.now(UTC))


def test_uid_under_another_uidvalidity_or_mailbox_is_another_message(store):
    ids = {put(store, 1), put(store, 1, uidvalidity=2), put(store, 1, address=B)}
    assert len(ids) == 3


def test_add_checks_status_and_addressed(store):
    with pytest.raises(ValueError, match="status"):
        put(store, 1, status="read")
    with pytest.raises(ValueError, match="addressed"):
        put(store, 1, addressed="bcc")
    assert store.search("")["total"] == 0


def test_message_round_trips_every_field(store):
    attachments = [Attachment(0, "简历-张三.pdf", "application/pdf", 20_480)]
    mid = store.add(
        A, 9, 5,
        parsed(
            message_id="<abc@host>", in_reply_to="<prev@host>",
            references=["<r0@h>", "<prev@host>"],
            date="2026-10-14T09:30:00+08:00", from_addr="hr@corp.example", from_name="人事部",
            to=[A, "x@y.example"], cc=["c@y.example"], subject="面试邀请",
            text="周三下午两点", attachments=attachments, bulk=True),
        "important", "cc", now=NOW)
    assert store.message(mid) == {
        "id": mid, "address": A, "uid": 9, "uidvalidity": 5, "message_id": "<abc@host>",
        "in_reply_to": "<prev@host>", "references": ["<r0@h>", "<prev@host>"],
        "date": "2026-10-14T01:30:00+00:00", "date_original": "2026-10-14T09:30:00+08:00",
        "from_addr": "hr@corp.example", "from_name": "人事部",
        "to": [A, "x@y.example"], "cc": ["c@y.example"], "subject": "面试邀请",
        "text": "周三下午两点",
        "attachments": [{"index": 0, "filename": "简历-张三.pdf",
                         "content_type": "application/pdf", "size": 20_480}],
        "bulk": True, "addressed": "cc", "status": "important",
        "added_at": iso(NOW), "judged_at": None, "pushed_at": None, "digested_at": None,
    }


def test_date_is_stored_in_utc_whatever_offset_the_sender_used(store):
    plus = put(store, 1, date="2026-10-14T23:30:00+08:00")
    minus = put(store, 2, date="2026-10-14T01:30:00-05:00")
    naive = put(store, 3, date="2026-10-14T04:00:00")
    odd = put(store, 4, date="not a date")
    missing = put(store, 5, date=None)
    far = put(store, 6, date="0001-01-01T00:00:00+08:00")  # cannot be moved to UTC
    assert store.message(plus)["date"] == "2026-10-14T15:30:00+00:00"
    assert store.message(minus)["date"] == "2026-10-14T06:30:00+00:00"
    assert store.message(naive)["date"] == "2026-10-14T04:00:00+00:00"
    for mid in (odd, missing, far):
        assert store.message(mid)["date"] is None
    assert store.message(odd)["date_original"] == "not a date"
    assert store.message(missing)["date_original"] is None


def test_text_that_cannot_be_encoded_does_not_stop_the_poll(store):
    mid = put(store, 1, subject="bad \udcff byte", text="x \ud800 y", from_name="\udc80")
    kept = store.message(mid)
    assert kept["subject"].startswith("bad ") and "\ud800" not in kept["text"]
    assert store.search("bad")["total"] == 1


def test_message_unknown_id(store):
    with pytest.raises(MailError, match="999"):
        store.message(999)


# -- judging ---------------------------------------------------------------------------------


def test_pending_lists_only_pending_oldest_first_with_paging(store):
    ids = [put(store, uid) for uid in range(1, 8)]
    put(store, 20, status="old")
    put(store, 21, status="digest")
    page1 = store.pending(3, 1)
    assert [m["id"] for m in page1["items"]] == ids[:3]
    assert page1["total"] == 7 and page1["next_page"] == 2
    assert page1["items"][0]["text"] == "see you on Wednesday"  # full messages
    page3 = store.pending(3, 3)
    assert [m["id"] for m in page3["items"]] == ids[6:] and page3["next_page"] is None
    assert store.pending(3, 4) == {"items": [], "total": 7, "next_page": None}


def test_judge_sets_statuses_and_records_pushes(store):
    a, b, c = put(store, 1), put(store, 2), put(store, 3)
    assert store.judge({a: "important", b: "digest"}, t(1)) == 2
    assert store.message(a)["status"] == "important"
    assert store.message(a)["judged_at"] == iso(t(1)) and store.message(a)["pushed_at"] == iso(t(1))
    assert store.message(b)["status"] == "digest"
    assert store.message(b)["judged_at"] == iso(t(1)) and store.message(b)["pushed_at"] is None
    assert store.message(c)["status"] == "pending"
    [push] = store.pushes()
    assert push["id"] == a and push["pushed_at"] == iso(t(1)) and push["subject"] == "message 1"
    assert [m["id"] for m in store.pending(10, 1)["items"]] == [c]


def test_judge_is_all_or_nothing_for_bad_input(store):
    a = put(store, 1)
    with pytest.raises(MailError, match="999"):
        store.judge({a: "important", 999: "digest"}, NOW)
    with pytest.raises(MailError, match="maybe"):
        store.judge({a: "maybe"}, NOW)
    with pytest.raises(MailError, match="pending"):  # not a verdict the store takes
        store.judge({a: "pending"}, NOW)
    assert store.message(a)["status"] == "pending" and store.pushes() == []


def test_judge_leaves_messages_that_are_not_pending_alone(store):
    a = put(store, 1)
    old = put(store, 2, status="old")
    assert store.judge({a: "important"}, t(1)) == 1
    # a retry of the same run, and a verdict on an old message: no second push, no change
    assert store.judge({a: "important", old: "important"}, t(2)) == 0
    assert store.message(old)["status"] == "old"
    assert len(store.pushes(limit=10)) == 1 and store.message(a)["pushed_at"] == iso(t(1))
    assert store.judge({}, t(3)) == 0


# -- the batch -------------------------------------------------------------------------------


def test_batch_wakes_once_then_again_after_ten_minutes(store):
    put(store, 1)
    assert store.batch_needs_wake(NOW)  # pending, never woken
    store.mark_woken(NOW)
    assert not store.batch_needs_wake(NOW)
    assert not store.batch_needs_wake(t(5))
    assert not store.batch_needs_wake(t(9.9))
    assert store.batch_needs_wake(t(10))
    assert store.batch_needs_wake(t(11))
    assert not store.batch_needs_wake(t(11), after_minutes=60)
    store.mark_woken(t(11))
    assert not store.batch_needs_wake(t(12))
    assert store.batch_needs_wake(t(22))


def test_batch_does_not_need_a_wake_when_nothing_is_pending(store):
    assert not store.batch_needs_wake(NOW)
    a = put(store, 1)
    store.mark_woken(NOW)
    store.judge({a: "digest"}, t(1))
    assert not store.batch_needs_wake(t(30))
    put(store, 2, status="digest")
    put(store, 3, status="old")
    assert not store.batch_needs_wake(t(30))


def test_mail_after_a_finished_batch_wakes_at_once(store):
    a = put(store, 1)
    store.mark_woken(NOW)
    store.judge({a: "digest"}, t(1))  # the batch is done
    put(store, 2)  # new mail two minutes after the wake
    assert store.batch_needs_wake(t(2))


def test_mail_joining_an_open_batch_waits_for_the_timeout(store):
    put(store, 1)
    store.mark_woken(NOW)
    put(store, 2)  # arrives while the woken batch is not yet done
    assert not store.batch_needs_wake(t(5))
    assert store.batch_needs_wake(t(10))


def test_a_wake_with_nothing_pending_does_not_hold_back_the_next_mail(store):
    store.mark_woken(NOW)  # woken for a mailbox problem only
    put(store, 1)
    assert store.batch_needs_wake(t(1))


def test_forgetting_the_only_pending_mailbox_ends_the_batch(store):
    put(store, 1)
    store.mark_woken(NOW)
    store.forget(A)
    put(store, 1, address=B)
    assert store.batch_needs_wake(t(1))


def test_open_batch(store):
    assert store.open_batch(NOW) is None
    put(store, 1)
    put(store, 2)
    assert store.open_batch(NOW) == {"opened_at": iso(NOW), "woken_at": None, "pending": 2}
    store.mark_woken(t(3))
    # the same batch, whenever it is asked for
    assert store.open_batch(t(4)) == {"opened_at": iso(NOW), "woken_at": iso(t(3)), "pending": 2}
    store.judge({m["id"]: "digest" for m in store.pending(10, 1)["items"]}, t(5))
    assert store.open_batch(t(6)) is None


# -- polls -----------------------------------------------------------------------------------


def test_poll_times_newest_first(store):
    assert store.poll_times() == []
    for minutes in (0, 5, 10):
        store.record_poll(t(minutes))
    assert store.poll_times() == [iso(t(10)), iso(t(5)), iso(t(0))]
    assert store.poll_times(limit=2) == [iso(t(10)), iso(t(5))]


def test_old_poll_times_are_dropped(store):
    for minutes in range(0, 5 * 250, 5):
        store.record_poll(t(minutes))
    kept = store.poll_times(limit=10_000)
    assert 100 <= len(kept) <= 250 and kept[0] == iso(t(5 * 249))


# -- problems --------------------------------------------------------------------------------


def test_first_problem_is_unreported_and_a_repeat_adds_no_row(store):
    pid = store.problem(A, "login", "wrong password", t(0))
    [first] = store.problems(unreported_only=True)
    assert first == {
        "id": pid, "address": A, "kind": "login", "message": "wrong password",
        "first_seen": iso(t(0)), "last_seen": iso(t(0)), "reported_at": None,
        "resolved_at": None, "state": "open",
    }
    assert store.problem(A, "login", "wrong password (again)", t(5)) == pid
    assert store.problem(A, "login", "wrong password (again)", t(10)) == pid
    [row] = store.problems(unreported_only=False)
    assert row["first_seen"] == iso(t(0)) and row["last_seen"] == iso(t(10))
    assert row["message"] == "wrong password (again)" and row["reported_at"] is None


def test_a_reported_problem_stays_listed_for_the_digest_but_not_as_new(store):
    pid = store.problem(A, "login", "wrong password", t(0))
    assert store.mark_reported([pid], t(1)) == 1
    store.problem(A, "login", "wrong password", t(5))  # seen again: still the same, still reported
    assert store.problems(unreported_only=True) == []
    [row] = store.problems(unreported_only=False)
    assert row["reported_at"] == iso(t(1)) and row["state"] == "open"


def test_resolve_yields_one_recovered_entry_until_it_is_reported(store):
    pid = store.problem(A, "login", "wrong password", t(0))
    store.mark_reported([pid], t(1))
    assert store.resolve(A, t(6)) == 1
    [entry] = store.problems(unreported_only=True)
    assert entry["id"] == pid and entry["state"] == "recovered"
    assert entry["resolved_at"] == iso(t(6)) and entry["reported_at"] == iso(t(1))
    assert store.problems(unreported_only=False) == [entry]
    assert store.resolve(A, t(11)) == 0  # nothing left to resolve: no second entry
    assert store.problems(unreported_only=True) == [entry]
    store.mark_reported([pid], t(12))
    assert store.problems(unreported_only=True) == []
    assert store.problems(unreported_only=False) == []


def test_a_problem_that_recovers_before_it_was_reported_is_one_entry(store):
    pid = store.problem(A, "connect", "timed out", t(0))
    store.resolve(A, t(5))
    [entry] = store.problems(unreported_only=True)
    assert entry["state"] == "recovered" and entry["reported_at"] is None
    store.mark_reported([pid], t(6))
    assert store.problems(unreported_only=False) == []


def test_a_problem_after_recovery_is_new_and_unreported(store):
    first = store.problem(A, "login", "wrong password", t(0))
    store.mark_reported([first], t(1))
    store.resolve(A, t(5))
    store.mark_reported([first], t(6))
    second = store.problem(A, "login", "wrong password", t(10))
    assert second != first
    [entry] = store.problems(unreported_only=True)
    assert entry["id"] == second and entry["state"] == "open" and entry["reported_at"] is None


def test_problems_are_per_mailbox_and_kind(store):
    login = store.problem(A, "login", "wrong password", t(0))
    connect = store.problem(A, "connect", "timed out", t(0))
    other = store.problem(B, "login", "wrong password", t(0))
    assert len({login, connect, other}) == 3
    store.resolve(A, t(5))  # the mailbox answered: both of its problems end, B's does not
    states = {p["id"]: p["state"] for p in store.problems(unreported_only=False)}
    assert states == {login: "recovered", connect: "recovered", other: "open"}


def test_mark_reported_ignores_unknown_ids(store):
    pid = store.problem(A, "login", "x", t(0))
    assert store.mark_reported([pid, 999], t(1)) == 1
    assert store.mark_reported([], t(1)) == 0


def test_last_ok_is_the_last_time_the_mailbox_answered(store):
    assert store.last_ok(A) is None
    store.set_baseline(A, 1, 5, None)
    assert store.last_ok(A) is None
    store.resolve(A, t(0))
    store.resolve(A, t(5))
    assert store.last_ok(A) == iso(t(5))
    store.set_baseline(A, 1, 6, None)  # moving the baseline does not forget it
    assert store.last_ok(A) == iso(t(5))
    assert store.last_ok(B) is None


# -- search ----------------------------------------------------------------------------------


def test_search_matches_chinese_substrings_in_subject_and_text(store):
    subject = put(store, 1, subject="阿里面试邀请", text="请准时参加")
    body = put(store, 2, subject="通知", text="周三下午两点面试，地点在三楼")
    put(store, 3, subject="发票", text="本月工资已发放")
    found = store.search("面试")
    assert found["total"] == 2 and {m["id"] for m in found["items"]} == {subject, body}
    assert [m["id"] for m in store.search("工资")["items"]] == [3]
    assert store.search("没有这个词")["items"] == []


def test_search_is_case_insensitive_for_ascii(store):
    mid = put(store, 1, subject="Quarterly REPORT", text="Invoice Number 7")
    for text in ("quarterly", "QUARTERLY", "Report", "invoice number", "NUMBER 7"):
        assert [m["id"] for m in store.search(text)["items"]] == [mid], text


def test_search_looks_at_sender_address_and_name_too(store):
    mid = put(store, 1, from_addr="Alice.Wong@Corp.Example", from_name="Alice 王", subject="x",
              text="y")
    put(store, 2, subject="x", text="y")
    assert [m["id"] for m in store.search("alice.wong@")["items"]] == [mid]
    assert [m["id"] for m in store.search("王")["items"]] == [mid]


def test_search_treats_wildcards_as_text(store):
    pct = put(store, 1, subject="100% sure")
    put(store, 2, subject="100x sure")
    under = put(store, 3, subject="a_b")
    put(store, 4, subject="aXb")
    slash = put(store, 5, subject="back\\slash")
    put(store, 6, subject="backslash")
    assert [m["id"] for m in store.search("%")["items"]] == [pct]
    assert [m["id"] for m in store.search("100% s")["items"]] == [pct]
    assert [m["id"] for m in store.search("_")["items"]] == [under]
    assert [m["id"] for m in store.search("a_b")["items"]] == [under]
    assert [m["id"] for m in store.search("\\")["items"]] == [slash]
    assert [m["id"] for m in store.search("back\\s")["items"]] == [slash]


def test_search_filters_by_mailbox_and_sender(store):
    a1 = put(store, 1, address=A, from_addr="boss@corp.example", from_name="Boss")
    b1 = put(store, 1, address=B, from_addr="shop@store.example", from_name="商店")
    a2 = put(store, 2, address=A, from_addr="shop@store.example", from_name="商店")
    assert {m["id"] for m in store.search("message")["items"]} == {a1, b1, a2}
    assert {m["id"] for m in store.search("message", address=A)["items"]} == {a1, a2}
    assert {m["id"] for m in store.search("message", address=B)["items"]} == {b1}
    assert {m["id"] for m in store.search("", sender="shop@store")["items"]} == {b1, a2}
    assert {m["id"] for m in store.search("", sender="商店")["items"]} == {b1, a2}
    assert {m["id"] for m in store.search("", sender="BOSS")["items"]} == {a1}
    assert [m["id"] for m in store.search("message", address=A, sender="shop")["items"]] == [a2]
    assert store.search("message", address="nobody@x.example")["total"] == 0


def test_search_since_compares_utc_instants(store):
    old = put(store, 1, date="2026-10-10T12:00:00+00:00")
    # 01:00 on the 12th in Beijing is still the 11th in UTC
    edge = put(store, 2, date="2026-10-12T01:00:00+08:00")
    new = put(store, 3, date="2026-10-14T00:00:00+00:00")
    nodate = put(store, 4, date=None, now=t(1))  # arrived at 12:01 on the 14th

    def ids(**kw):
        return [m["id"] for m in store.search("", **kw)["items"]]

    assert ids(since=date(2026, 10, 12)) == [nodate, new]
    assert ids(since="2026-10-12") == [nodate, new]
    assert ids(since=date(2026, 10, 11)) == [nodate, new, edge]
    assert ids(since=datetime(2026, 10, 14, 8, 0, tzinfo=timezone(timedelta(hours=8)))) == [
        nodate, new]  # 08:00 in Beijing is 00:00 UTC
    assert ids(since="2026-10-14T00:00:00+00:00") == [nodate, new]
    assert ids(since=date(2026, 10, 1)) == [nodate, new, edge, old]
    assert ids(since=date(2026, 10, 15)) == []


def test_search_since_rejects_a_bad_date(store):
    with pytest.raises(MailError, match="since"):
        store.search("", since="last week")


def test_search_pages_of_ten_newest_first(store):
    ids = [put(store, n, subject=f"report {n}", date=f"2026-09-{n:02d}T10:00:00+00:00")
           for n in range(1, 26)]
    first = store.search("report")
    assert first["total"] == 25 and first["next_page"] == 2
    assert [m["id"] for m in first["items"]] == ids[::-1][:10]
    second = store.search("report", page=2)
    assert [m["id"] for m in second["items"]] == ids[::-1][10:20] and second["next_page"] == 3
    third = store.search("report", page=3)
    assert [m["id"] for m in third["items"]] == ids[::-1][20:] and third["next_page"] is None
    assert store.search("report", page=4) == {"items": [], "total": 25, "next_page": None}
    five = store.search("report", per_page=5, page=5)
    assert len(five["items"]) == 5 and five["next_page"] is None
    for bad in ({"page": 0}, {"per_page": 0}):
        with pytest.raises(MailError):
            store.search("report", **bad)


def test_search_items_are_summaries_without_the_body(store):
    mid = put(store, 1, subject="offer", text="long body " * 100,
              attachments=[Attachment(0, "offer.pdf", "application/pdf", 9)])
    [item] = store.search("offer")["items"]
    assert item == {
        "id": mid, "address": A, "date": "2026-10-14T01:30:00+00:00",
        "from_addr": "boss@corp.example", "from_name": "Boss", "subject": "offer",
        "status": "pending",
        "attachments": [{"index": 0, "filename": "offer.pdf", "content_type": "application/pdf",
                         "size": 9}],
    }


def test_search_covers_every_status_and_an_empty_text_lists_all(store):
    for uid, status in enumerate(("old", "pending", "important", "digest", "digested"), 1):
        put(store, uid, status=status)
    assert store.search("")["total"] == 5 and store.search("   ")["total"] == 5


# -- pushes ----------------------------------------------------------------------------------


def test_pushes_newest_first_and_limited(store):
    ids = [put(store, n) for n in range(1, 8)]
    for n, mid in enumerate(ids):
        store.judge({mid: "important"}, t(n))
    recent = store.pushes()
    assert [p["id"] for p in recent] == ids[::-1][:5]
    assert recent[0]["pushed_at"] == iso(t(6)) and recent[0]["from_addr"] == "boss@corp.example"
    assert len(store.pushes(limit=2)) == 2 and len(store.pushes(limit=50)) == 7


def test_push_count_since(store):
    ids = [put(store, n) for n in range(1, 5)]
    for n, mid in enumerate(ids):
        store.judge({mid: "important"}, t(n))
    assert store.push_count() == 4
    assert store.push_count(since=iso(t(1))) == 2  # strictly after
    assert store.push_count(since=t(3)) == 0


# -- digest ----------------------------------------------------------------------------------


def test_digest_items_and_mark_digested(store):
    assert store.last_digest() is None
    routed = put(store, 1, status="digest")
    judged = put(store, 2, status="pending")
    important = put(store, 3, status="pending")
    put(store, 4, status="old")
    pending = put(store, 5, status="pending")
    other = put(store, 1, address=B, status="digest")
    store.judge({judged: "digest", important: "important"}, t(1))

    listing = store.digest_items(1, 10)
    assert listing["total"] == 3 and listing["next_page"] is None
    # grouped by mailbox (addresses sort as text: "me+shop@" before "me@"), arrival order
    # inside a mailbox; full messages for the agent
    assert [m["id"] for m in listing["items"]] == [other, routed, judged]
    assert listing["items"][0]["text"] == "see you on Wednesday"

    assert store.mark_digested(t(60)) == 3
    assert store.last_digest() == iso(t(60))
    assert store.digest_items(1, 10) == {"items": [], "total": 0, "next_page": None}
    assert store.message(routed)["status"] == "digested"
    assert store.message(routed)["digested_at"] == iso(t(60))
    # nothing else moved
    assert store.message(important)["status"] == "important"
    assert store.message(pending)["status"] == "pending"
    assert store.search("message 4")["items"][0]["status"] == "old"
    # a digest with nothing in it still counts as a digest
    assert store.mark_digested(t(120)) == 0 and store.last_digest() == iso(t(120))


def test_digest_items_page(store):
    ids = [put(store, n, status="digest") for n in range(1, 13)]
    first = store.digest_items(1, 5)
    assert [m["id"] for m in first["items"]] == ids[:5]
    assert first["total"] == 12 and first["next_page"] == 2
    assert store.digest_items(3, 5)["next_page"] is None
    assert len(store.digest_items(3, 5)["items"]) == 2


# -- shape -----------------------------------------------------------------------------------


def test_everything_returned_is_plain_json(store):
    a = put(store, 1, attachments=[Attachment(0, "a.pdf", "application/pdf", 1)])
    put(store, 2, status="digest")
    pid = store.problem(A, "login", "x", NOW)
    store.mark_reported([pid], NOW)
    store.resolve(A, t(1))
    store.record_poll(NOW)
    store.judge({a: "important"}, NOW)
    store.open_batch(NOW)
    results = [
        store.search("message"), store.message(a), store.pending(10, 1), store.pushes(),
        store.digest_items(1, 10), store.problems(unreported_only=False), store.poll_times(),
        store.open_batch(NOW), store.state(A),
    ]
    assert json.loads(json.dumps(results, ensure_ascii=False)) == results

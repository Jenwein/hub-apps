"""The mailbox actions: mark read or unread, archive, delete lists and delete (always to Trash),
attachment download. Each runs on an index filled by a real poll with FakeServers, so what the
server was told (`FakeServer.calls`) and what the index remembers are both checked.

The first mailbox is `工作` at `me+x@corp.example`: a Chinese short name and a `+tag` address
go through the config, the secrets path, the command line and the attachment path."""

import io
import json
import re
import shutil
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from mailfakes import FakeServer, fake_connect, make_raw
from mailkit import accounts, actions, background, cli, poll, server
from mailkit.config import MailError, read_text, remove_mailbox_text, write_text
from mailkit.server import ServerError
from mailkit.store import Store

SEED_MAIL = Path(__file__).parents[1] / "mail" / "seed" / "mail"
NOW = datetime(2026, 10, 14, 4, 0, tzinfo=UTC)
WORK = "me+x@corp.example"
QQ = "me@qq.com"
GMAIL = "me@gmail.com"
PASSWORD = "s3cretpass"
TRASH = ("\\Trash",)


@pytest.fixture
def hub_dir(tmp_path):
    hub = tmp_path / "hub"
    shutil.copytree(SEED_MAIL, hub / "mail")
    (hub / "secrets").mkdir()
    return hub


def mail(hub):
    return hub / "mail"


def add(hub, fake, name="工作", address=WORK, provider="feishu"):
    result = accounts.add(hub, mail(hub), name, address, PASSWORD, provider=provider,
                          connect=fake_connect(fake), now=NOW)
    assert result["ok"], result


def raw(subject="hi", sender="boss@corp.example", to=WORK, **options):
    return make_raw(subject, sender, to, f"body of {subject}", **options)


class World:
    """Two mailboxes, each on its own FakeServer: 工作 (Feishu, folders Trash and Archive, with
    SPECIAL-USE flags) and QQ (folders 已删除 and 归档, flagged the same way)."""

    def __init__(self, hub):
        self.hub = hub
        self.work = FakeServer(
            folders={"Trash": {}, "Archive": {}},
            folder_flags={"Trash": TRASH, "Archive": ("\\Archive",)})
        self.qq = FakeServer(
            folders={"已删除": {}, "归档": {}},
            folder_flags={"已删除": TRASH, "归档": ("\\Archive",)})
        add(hub, self.work)
        add(hub, self.qq, "QQ", QQ, "qq")
        self.servers = {WORK: self.work, QQ: self.qq}
        self.connects: list[str] = []

    @property
    def mail(self):
        return mail(self.hub)

    def connect(self):
        """A `connect` that records the mailbox of each connection in `self.connects`."""
        inner = fake_connect(self.servers)
        self.connects = []

        def connect(mb, password, **options):
            self.connects.append(mb.name)
            return inner(mb, password, **options)

        return connect

    def deliver(self, subject, *, server=None, **options) -> int:
        """A new INBOX message (default: in 工作); returns its UID."""
        server = server or self.work
        to = QQ if server is self.qq else WORK
        return server.deliver(raw(subject, to=to, **options))

    def poll(self, minutes=1):
        poll.poll(self.hub, self.mail, connect=fake_connect(self.servers),
                  now=NOW + timedelta(minutes=minutes))

    def ids(self) -> dict[str, int]:
        """The id of each indexed message by subject."""
        with Store.open(self.mail) as store:
            return {m["subject"]: m["id"] for m in store.search("", per_page=1000)["items"]}

    def stored(self, mid) -> dict:
        with Store.open(self.mail) as store:
            return store.message(mid)

    def flag(self, ids, seen=True):
        return actions.flag(self.hub, self.mail, ids, seen, connect=self.connect())

    def archive(self, ids, **options):
        return actions.archive(self.hub, self.mail, ids, connect=self.connect(), **options)

    def delete_list(self, ids):
        return actions.delete_list(self.mail, ids)

    def delete(self, list_id):
        return actions.delete(self.hub, self.mail, list_id, connect=self.connect())

    def download(self, mid, index=0):
        return actions.download_attachment(
            self.hub, self.mail, mid, index, connect=self.connect())


@pytest.fixture
def world(hub_dir):
    return World(hub_dir)


def silent(*servers):
    """Assert that none of the servers was written to."""
    for srv in servers:
        assert srv.writes() == []


# -- flag ------------------------------------------------------------------------------------


def test_flag_sets_and_clears_seen_in_each_mailbox_with_one_connection_each(world):
    a1, a2 = world.deliver("a1"), world.deliver("a2")
    b = world.deliver("b", server=world.qq)
    world.poll()
    ids = world.ids()

    out = world.flag([ids["a1"], ids["b"], ids["a2"]])
    assert out == {
        "ok": True, "seen": True, "done": 3, "ids": [ids["a1"], ids["b"], ids["a2"]],
        "mailboxes": [{"mailbox": "工作", "count": 2}, {"mailbox": "QQ", "count": 1}],
        "refused": [], "errors": [],
    }
    assert world.connects == ["工作", "QQ"]  # one connection per mailbox
    assert world.work.flags == {a1: {"\\Seen"}, a2: {"\\Seen"}}
    assert world.qq.flags == {b: {"\\Seen"}}
    assert world.work.writes() == [("set_seen", (a1, a2), True)]
    assert world.work.closed and world.qq.closed

    out = world.flag([ids["a1"]], seen=False)
    assert (out["ok"], out["seen"], out["done"]) == (True, False, 1)
    assert world.work.flags == {a1: set(), a2: {"\\Seen"}}
    json.dumps(out, ensure_ascii=False)


def test_flag_repeats_and_duplicate_ids_are_harmless(world):
    uid = world.deliver("a")
    world.poll()
    (mid,) = world.ids().values()
    out = world.flag([mid, mid, mid])
    assert (out["done"], out["ids"]) == (1, [mid])
    assert world.work.writes() == [("set_seen", (uid,), True)]


def test_flag_one_mailbox_failing_does_not_stop_the_others(world):
    a = world.deliver("a")
    world.deliver("b", server=world.qq)
    world.poll()
    ids = world.ids()
    world.qq.error = ServerError(f"NO login {PASSWORD} failed")

    out = world.flag([ids["a"], ids["b"]])
    assert out["ok"] is False and out["done"] == 1 and out["ids"] == [ids["a"]]
    (error,) = out["errors"]
    assert (error["mailbox"], error["ids"]) == ("QQ", [ids["b"]])
    assert "[password]" in error["error"] and PASSWORD not in json.dumps(out)
    assert world.work.flags == {a: {"\\Seen"}}


def test_flag_login_failure_is_an_error_entry_for_that_mailbox(world):
    world.deliver("a")
    world.poll()
    (mid,) = world.ids().values()
    world.work.fail_login = "imap-login"
    out = world.flag([mid])
    assert out["ok"] is False and out["done"] == 0
    assert out["errors"][0]["mailbox"] == "工作"
    assert "refused the login" in out["errors"][0]["error"]


def test_flag_without_a_stored_password_is_an_error_entry(world):
    world.deliver("a")
    world.poll()
    (mid,) = world.ids().values()
    accounts.secret_path(world.hub, WORK).unlink()
    out = world.flag([mid])
    assert out["ok"] is False and "no password stored" in out["errors"][0]["error"]
    assert world.connects == []


def test_flag_refuses_unknown_ids_without_touching_a_server(world):
    out = world.flag([99])
    assert out["ok"] is False and out["done"] == 0
    assert out["refused"] == [{"id": 99, "reason": "no message with id 99"}]
    assert world.connects == []
    silent(world.work)


def test_flag_refuses_a_message_that_has_left_inbox(world):
    world.deliver("a")
    world.poll()
    (mid,) = world.ids().values()
    world.archive([mid])
    out = world.flag([mid])
    assert out["ok"] is False and "already moved to Archive" in out["refused"][0]["reason"]
    assert [call[0] for call in world.work.writes()] == ["move"]


def test_flag_needs_at_least_one_id(world):
    with pytest.raises(MailError, match="at least one message id"):
        world.flag([])


# -- stale UIDVALIDITY ----------------------------------------------------------------------


def test_stale_uidvalidity_in_the_index_refuses_the_message_and_never_asks_the_server(world):
    world.deliver("old one")
    world.poll()
    (old,) = world.ids().values()
    world.work.uidvalidity = 7  # the server renumbered INBOX; the next poll notices
    world.poll(2)
    world.work.calls.clear()

    for result in (world.flag([old]), world.archive([old])):
        assert result["ok"] is False and result["done"] == 0
        (refusal,) = result["refused"]
        assert refusal["id"] == old and "search again" in refusal["reason"]
    with pytest.raises(MailError, match="search again"):
        world.delete_list([old])
    assert world.connects == [] and world.work.calls == []


def test_stale_uidvalidity_on_the_server_refuses_before_any_change(world):
    uid = world.deliver("a")
    world.deliver("b", server=world.qq)
    world.poll()
    ids = world.ids()
    world.work.uidvalidity = 5  # renumbered since the last poll; the index does not know yet

    out = world.archive([ids["a"], ids["b"]])
    assert out["done"] == 1 and out["ids"] == [ids["b"]]
    assert [r["id"] for r in out["refused"]] == [ids["a"]]
    assert "search again" in out["refused"][0]["reason"]
    silent(world.work)
    assert uid in world.work.inbox
    assert world.stored(ids["a"])["moved_to"] is None
    assert world.stored(ids["b"])["moved_to"] == "归档"


# -- archive ---------------------------------------------------------------------------------


def test_archive_moves_to_the_folder_flagged_archive_whatever_its_name(world):
    a = world.deliver("a")
    b = world.deliver("b", server=world.qq)
    world.poll()
    ids = world.ids()

    out = world.archive([ids["a"], ids["b"]])
    assert out["ok"] is True and out["done"] == 2 and out["refused"] == [] and out["errors"] == []
    assert out["mailboxes"] == [
        {"mailbox": "工作", "count": 1, "folder": "Archive", "created": False, "expunged": True},
        {"mailbox": "QQ", "count": 1, "folder": "归档", "created": False, "expunged": True},
    ]
    assert out["warnings"] == []
    assert world.work.writes() == [("move", (a,), "Archive")]
    assert world.qq.writes() == [("move", (b,), "归档")]
    assert a not in world.work.inbox and len(world.work.folders_["Archive"]) == 1
    assert len(world.qq.folders_["归档"]) == 1
    # the index remembers where each went
    one = world.stored(ids["a"])
    assert (one["moved_to"], one["moved_expunged"]) == ("Archive", True)
    assert world.stored(ids["b"])["moved_to"] == "归档"


def test_archive_tells_search_where_the_message_went(world):
    world.deliver("offer letter")
    world.poll()
    (mid,) = world.ids().values()
    world.archive([mid])
    with Store.open(world.mail) as store:
        (item,) = store.search("offer")["items"]
    assert item["moved_to"] == "Archive"


def test_mail_the_user_deleted_or_archived_is_not_pushed_or_digested(world):
    world.deliver("to delete")
    world.deliver("to archive")
    world.deliver("kept")
    world.poll()
    ids = world.ids()
    assert background.pending(world.mail)["total"] == 3
    world.delete(world.delete_list([ids["to delete"]])["list_id"])
    world.archive([ids["to archive"]])
    # triage runs after the user acted: only the mail still in INBOX is waiting for a verdict
    pending = background.pending(world.mail)
    assert [item["id"] for item in pending["items"]] == [ids["kept"]] and pending["total"] == 1
    stale = [f"{ids['to delete']}=important", f"{ids['to archive']}=digest"]
    assert background.judge(world.mail, stale, now=NOW) == {"judged": 0, "pending_left": 1}
    assert background.recent_pushed(world.mail)["items"] == []
    assert background.digest_items(world.mail, now=NOW)["total"] == 0


def test_archiving_the_same_message_twice_is_refused_and_copies_nothing_again(world):
    world.deliver("a")
    world.poll()
    (mid,) = world.ids().values()
    assert world.archive([mid])["done"] == 1
    again = world.archive([mid])
    assert again["ok"] is False and again["done"] == 0
    (refusal,) = again["refused"]
    assert refusal["id"] == mid and "already moved to Archive" in refusal["reason"]
    assert len(world.work.folders_["Archive"]) == 1
    assert len(world.work.writes()) == 1  # the second call never reached the server


def test_archive_of_a_message_already_deleted_is_refused(world):
    world.deliver("a")
    world.poll()
    (mid,) = world.ids().values()
    world.delete(world.delete_list([mid])["list_id"])
    refusal = world.archive([mid])["refused"][0]
    assert "already moved to Trash" in refusal["reason"]
    assert world.work.folders_["Archive"] == {}


def test_archive_mixes_new_and_already_moved_ids(world):
    world.deliver("a")
    world.deliver("b")
    world.poll()
    ids = world.ids()
    world.archive([ids["a"]])
    out = world.archive([ids["a"], ids["b"]])
    assert out["ok"] is False and out["done"] == 1 and out["ids"] == [ids["b"]]
    assert [r["id"] for r in out["refused"]] == [ids["a"]]
    assert len(world.work.folders_["Archive"]) == 2  # one each, none twice


def gmail_world(hub, folders, flags):
    gmail = FakeServer(folders=folders, folder_flags=flags)
    add(hub, gmail, "Gmail", GMAIL, "gmail")
    return gmail


def test_gmail_archives_to_all_mail_found_by_its_flag(hub_dir):
    # a German account: the folder names are translated, only the flag tells which is which
    gmail = gmail_world(
        hub_dir,
        {"[Gmail]/Alle Nachrichten": {}, "[Gmail]/Papierkorb": {}, "Archive": {}},
        {"[Gmail]/Alle Nachrichten": ("\\All",), "[Gmail]/Papierkorb": TRASH,
         "Archive": ("\\Archive",)})
    uid = gmail.deliver(raw("hello", to=GMAIL))
    poll.poll(hub_dir, mail(hub_dir), connect=fake_connect(gmail), now=NOW)
    with Store.open(mail(hub_dir)) as store:
        (mid,) = (m["id"] for m in store.search("")["items"])
    out = actions.archive(hub_dir, mail(hub_dir), [mid], connect=fake_connect(gmail))
    assert out["ok"] and out["mailboxes"][0]["folder"] == "[Gmail]/Alle Nachrichten"
    assert gmail.writes() == [("move", (uid,), "[Gmail]/Alle Nachrichten")]


def test_gmail_falls_back_to_the_provider_name_when_there_is_no_flag(hub_dir):
    gmail = gmail_world(hub_dir, {"[Gmail]/All Mail": {}, "[Gmail]/Trash": {}}, {})
    gmail.deliver(raw("hello", to=GMAIL))
    poll.poll(hub_dir, mail(hub_dir), connect=fake_connect(gmail), now=NOW)
    with Store.open(mail(hub_dir)) as store:
        (mid,) = (m["id"] for m in store.search("")["items"])
    out = actions.archive(hub_dir, mail(hub_dir), [mid], connect=fake_connect(gmail))
    assert out["ok"] and out["mailboxes"][0]["folder"] == "[Gmail]/All Mail"
    # a Gmail account never archives to a folder that is merely flagged \Archive
    assert gmail.folders_["[Gmail]/All Mail"] != {}


def test_a_gmail_style_account_ignores_an_archive_flag_and_needs_all_mail(hub_dir):
    gmail = gmail_world(hub_dir, {"Elsewhere": {}}, {"Elsewhere": ("\\Archive",)})
    gmail.deliver(raw("hello", to=GMAIL))
    poll.poll(hub_dir, mail(hub_dir), connect=fake_connect(gmail), now=NOW)
    with Store.open(mail(hub_dir)) as store:
        (mid,) = (m["id"] for m in store.search("")["items"])
    out = actions.archive(hub_dir, mail(hub_dir), [mid], connect=fake_connect(gmail))
    assert out["ok"] is False and "--create" in out["errors"][0]["error"]
    silent(gmail)


def test_archive_without_an_archive_folder_asks_for_create_and_moves_nothing(world):
    quiet = FakeServer()  # no folders at all
    world.servers[WORK] = quiet
    uid = world.deliver("a", server=quiet)
    world.poll()
    (mid,) = world.ids().values()
    out = world.archive([mid])
    assert out["ok"] is False and out["done"] == 0
    (error,) = out["errors"]
    assert error["mailbox"] == "工作" and error["ids"] == [mid]
    assert "--create" in error["error"] and "ask the user" in error["error"]
    assert "Archive" in error["error"]
    silent(quiet)
    assert uid in quiet.inbox and world.stored(mid)["moved_to"] is None


def test_archive_with_create_makes_the_archive_folder_then_moves(world):
    quiet = FakeServer()
    world.servers[WORK] = quiet
    uid = world.deliver("a", server=quiet)
    world.poll()
    (mid,) = world.ids().values()
    out = world.archive([mid], create=True)
    assert out["ok"] is True
    assert out["mailboxes"] == [
        {"mailbox": "工作", "count": 1, "folder": "Archive", "created": True, "expunged": True}]
    assert quiet.writes() == [("create_folder", "Archive"), ("move", (uid,), "Archive")]
    assert world.stored(mid)["moved_to"] == "Archive"


def test_archive_with_create_uses_a_folder_named_archive_that_already_exists(world):
    plain = FakeServer(folders={"archive": {}})  # exists, but the server does not flag it
    world.servers[WORK] = plain
    uid = world.deliver("a", server=plain)
    world.poll()
    (mid,) = world.ids().values()
    refused = world.archive([mid])
    assert refused["ok"] is False
    assert "--create" in refused["errors"][0]["error"]
    assert "archive" in refused["errors"][0]["error"].lower()
    silent(plain)

    out = world.archive([mid], create=True)
    assert out["ok"] is True and out["mailboxes"][0]["folder"] == "archive"
    assert out["mailboxes"][0]["created"] is False
    assert plain.writes() == [("move", (uid,), "archive")]


def test_archive_on_a_server_that_cannot_move_says_the_originals_stay_in_inbox(world):
    world.work.caps = set()  # neither MOVE nor UIDPLUS
    uid = world.deliver("a")
    world.poll()
    (mid,) = world.ids().values()
    out = world.archive([mid])
    assert out["ok"] is True and out["done"] == 1
    assert out["mailboxes"][0]["expunged"] is False
    (warning,) = out["warnings"]
    assert "工作" in warning and "INBOX" in warning and "flagged deleted" in warning
    assert "expunge" in warning
    # the server kept the original, flagged; the index still records the move
    assert uid in world.work.inbox and "\\Deleted" in world.work.flags[uid]
    moved = world.stored(mid)
    assert (moved["moved_to"], moved["moved_expunged"]) == ("Archive", False)
    # and the same message is not copied again
    assert "already moved to Archive" in world.archive([mid])["refused"][0]["reason"]
    assert len(world.work.folders_["Archive"]) == 1


def test_archive_failing_mailbox_is_an_error_entry_and_the_message_stays(world):
    a = world.deliver("a")
    b = world.deliver("b", server=world.qq)
    world.poll()
    ids = world.ids()
    world.work.error = ServerError("connection reset")
    out = world.archive([ids["a"], ids["b"]])
    assert out["ok"] is False and out["done"] == 1 and out["ids"] == [ids["b"]]
    assert out["errors"][0]["mailbox"] == "工作" and "connection reset" in out["errors"][0]["error"]
    assert world.stored(ids["a"])["moved_to"] is None and a in world.work.inbox
    assert world.stored(ids["b"])["moved_to"] == "归档" and b not in world.qq.inbox


def test_a_move_the_index_cannot_record_is_still_reported_as_done(world, monkeypatch):
    uid = world.deliver("a")
    world.poll()
    (mid,) = world.ids().values()

    def busy(*_args, **_kwargs):
        raise MailError("the mail index is busy")

    monkeypatch.setattr(Store, "mark_moved", busy)
    out = world.archive([mid])
    assert out["ok"] is True and out["done"] == 1  # the mail did move
    (warning,) = out["warnings"]
    assert "could not record it" in warning and "do not move them again" in warning
    assert uid not in world.work.inbox


def test_a_mailbox_that_left_config_toml_is_an_error_entry(world):
    world.deliver("a")
    world.poll()
    (mid,) = world.ids().values()
    text = read_text(world.mail)
    write_text(world.mail, remove_mailbox_text(text, "工作"))
    out = world.flag([mid])
    assert out["ok"] is False and out["done"] == 0
    assert out["errors"][0]["mailbox"] == WORK and "not in config.toml" in out["errors"][0]["error"]
    silent(world.work)


# -- delete lists ----------------------------------------------------------------------------


def lists_dir(world):
    return world.mail / ".state" / "delete-lists"


def test_delete_list_of_25_ids_shows_10_and_says_25(world):
    for n in range(25):
        world.deliver(f"mail {n:02d}")
    world.poll()
    ids = sorted(world.ids().values())

    out = world.delete_list(ids)
    assert set(out) == {"list_id", "total", "items", "next_page"}
    assert re.fullmatch(r"[a-z0-9]{8}", out["list_id"])
    assert out["total"] == 25 and len(out["items"]) == 10 and out["next_page"] == 2
    assert [i["id"] for i in out["items"]] == ids[:10]
    assert set(out["items"][0]) == {"id", "mailbox", "from", "from_name", "subject", "date"}
    assert out["items"][0]["mailbox"] == "工作" and out["items"][0]["subject"] == "mail 00"
    json.dumps(out, ensure_ascii=False)

    # the exact set is on disk, with where each message is on the server
    (saved,) = lists_dir(world).iterdir()
    assert saved.name == f"{out['list_id']}.json"
    data = json.loads(saved.read_text("utf-8"))
    assert [i["id"] for i in data["items"]] == ids
    with Store.open(world.mail) as store:
        for entry in data["items"]:
            row = store.message(entry["id"])
            assert (entry["address"], entry["uid"], entry["uidvalidity"]) == (
                row["address"], row["uid"], row["uidvalidity"])
    assert world.connects == [] and world.work.writes() == []  # listing changes nothing


def test_delete_list_pages_through_the_saved_set(world):
    for n in range(25):
        world.deliver(f"mail {n:02d}")
    world.poll()
    ids = sorted(world.ids().values())
    list_id = world.delete_list(ids)["list_id"]

    second = actions.show_delete_list(world.mail, list_id, page=2)
    assert [i["id"] for i in second["items"]] == ids[10:20]
    assert (second["total"], second["next_page"], second["list_id"]) == (25, 3, list_id)
    last = actions.show_delete_list(world.mail, list_id, page=3)
    assert [i["id"] for i in last["items"]] == ids[20:] and last["next_page"] is None
    assert actions.show_delete_list(world.mail, list_id, page=4)["items"] == []
    with pytest.raises(MailError, match="at least 1"):
        actions.show_delete_list(world.mail, list_id, page=0)


def test_each_delete_list_has_its_own_id(world):
    world.deliver("a")
    world.poll()
    (mid,) = world.ids().values()
    first, second = world.delete_list([mid]), world.delete_list([mid])
    assert first["list_id"] != second["list_id"]
    assert len(list(lists_dir(world).iterdir())) == 2


def test_delete_list_refuses_ids_it_cannot_list_exactly(world):
    world.deliver("a")
    world.deliver("b")
    world.poll()
    ids = world.ids()
    with pytest.raises(MailError, match="no message with id 99"):
        world.delete_list([ids["a"], 99])
    world.archive([ids["b"]])
    with pytest.raises(MailError, match="already moved to Archive"):
        world.delete_list([ids["a"], ids["b"]])
    with pytest.raises(MailError, match="at least one message id"):
        world.delete_list([])
    assert not lists_dir(world).exists() or list(lists_dir(world).iterdir()) == []


def test_delete_list_counts_a_repeated_id_once(world):
    world.deliver("a")
    world.poll()
    (mid,) = world.ids().values()
    out = world.delete_list([mid, mid])
    assert out["total"] == 1 and len(out["items"]) == 1


def test_delete_list_names_the_address_when_the_config_is_gone(world):
    world.deliver("a")
    world.poll()
    (mid,) = world.ids().values()
    (world.mail / "config.toml").unlink()
    assert world.delete_list([mid])["items"][0]["mailbox"] == WORK


# -- delete ----------------------------------------------------------------------------------


def test_delete_of_an_unknown_list_id_fails(world):
    with pytest.raises(MailError, match="no delete list zz9plura"):
        world.delete("zz9plura")
    assert world.connects == []


@pytest.mark.parametrize("bad", ["../x", "..\\..\\x", "", "ABCDEFGH", "abc", "abcdefghi",
                                 "a b c d e", "ab\u0663defgh", "abcdefg."])
def test_delete_list_ids_that_are_not_ours_never_reach_the_file_system(world, bad):
    with pytest.raises(MailError, match="not a delete list id"):
        world.delete(bad)
    with pytest.raises(MailError, match="not a delete list id"):
        actions.show_delete_list(world.mail, bad)


def test_delete_moves_exactly_the_listed_messages_to_trash_and_nothing_else(world):
    uids = [world.deliver(f"mail {n}") for n in range(5)]
    other = world.deliver("elsewhere", server=world.qq)
    world.poll()
    ids = world.ids()
    listed = [ids["mail 1"], ids["mail 3"]]
    list_id = world.delete_list(listed)["list_id"]

    out = world.delete(list_id)
    assert out == {
        "ok": True, "list_id": list_id, "done": 2, "ids": listed,
        "mailboxes": [{"mailbox": "工作", "count": 2, "folder": "Trash", "expunged": True}],
        "refused": [], "errors": [], "warnings": [], "remaining": 0,
    }
    # only a move to Trash, of exactly those two: no flags, no expunge, nothing in QQ
    assert world.work.writes() == [("move", (uids[1], uids[3]), "Trash")]
    assert world.work.flags == {}
    assert sorted(world.work.inbox) == [uids[0], uids[2], uids[4]]
    assert len(world.work.folders_["Trash"]) == 2
    assert world.qq.writes() == [] and other in world.qq.inbox
    for subject, mid in ids.items():
        moved = world.stored(mid)["moved_to"]
        assert moved == ("Trash" if mid in listed else None), subject
    # the list is used up
    assert list(lists_dir(world).iterdir()) == []
    with pytest.raises(MailError, match="no delete list"):
        world.delete(list_id)
    assert len(world.work.folders_["Trash"]) == 2


def test_delete_on_a_server_that_cannot_move_says_the_original_stays_in_inbox(world):
    world.work.caps = set()  # the server can neither MOVE nor UID EXPUNGE
    uid = world.deliver("a")
    world.poll()
    (mid,) = world.ids().values()
    out = world.delete(world.delete_list([mid])["list_id"])
    assert out["ok"] is True and out["mailboxes"][0]["expunged"] is False
    (warning,) = out["warnings"]
    assert "INBOX" in warning and "flagged deleted" in warning and "expunge" in warning
    # the app asks for a move and nothing else (no flag, no expunge of its own); this server can
    # only copy, and the adapter says the original stays
    assert [call[0] for call in world.work.writes()] == ["move"]
    assert len(world.work.folders_["Trash"]) == 1  # the copy is in Trash
    assert uid in world.work.inbox  # and the original is flagged, as the server could do no better
    assert world.stored(mid)["moved_expunged"] is False


def test_delete_finds_gmails_trash_by_its_name_when_no_flag_says_so(hub_dir):
    gmail = gmail_world(hub_dir, {"[Gmail]/All Mail": {}, "[Gmail]/Trash": {}}, {})
    gmail.deliver(raw("hello", to=GMAIL))
    poll.poll(hub_dir, mail(hub_dir), connect=fake_connect(gmail), now=NOW)
    with Store.open(mail(hub_dir)) as store:
        (mid,) = (m["id"] for m in store.search("")["items"])
    listed = actions.delete_list(mail(hub_dir), [mid])
    out = actions.delete(hub_dir, mail(hub_dir), listed["list_id"], connect=fake_connect(gmail))
    assert out["ok"] and out["mailboxes"][0]["folder"] == "[Gmail]/Trash"


def test_delete_in_a_mailbox_with_a_chinese_trash_folder(world):
    b = world.deliver("b", server=world.qq)
    world.poll()
    (mid,) = world.ids().values()
    out = world.delete(world.delete_list([mid])["list_id"])
    assert out["mailboxes"][0]["folder"] == "已删除"
    assert world.qq.writes() == [("move", (b,), "已删除")]


def test_delete_without_a_trash_folder_moves_nothing_and_keeps_the_list(world):
    bare = FakeServer(folders={"Archive": {}}, folder_flags={"Archive": ("\\Archive",)})
    world.servers[WORK] = bare
    uid = world.deliver("a", server=bare)
    world.poll()
    (mid,) = world.ids().values()
    listed = world.delete_list([mid])
    out = world.delete(listed["list_id"])
    assert out["ok"] is False and out["done"] == 0
    (error,) = out["errors"]
    assert error["mailbox"] == "工作" and error["ids"] == [mid]
    assert "Trash" in error["error"] and "nothing was deleted" in error["error"]
    silent(bare)  # in particular: no \Deleted flag and no expunge as a fallback
    assert uid in bare.inbox and bare.flags == {}
    assert world.stored(mid)["moved_to"] is None
    assert out["remaining"] == 1 and out["list_id"] == listed["list_id"]
    # the list is still there to try again once the user has a Trash folder
    bare.folders_["Trash"] = {}
    bare.folder_flags["Trash"] = TRASH
    again = world.delete(listed["list_id"])
    assert again["ok"] is True and again["remaining"] == 0
    assert bare.writes() == [("move", (uid,), "Trash")]


def test_delete_one_mailbox_failing_keeps_only_its_messages_in_the_list(world):
    a = world.deliver("a")
    b = world.deliver("b", server=world.qq)
    world.poll()
    ids = world.ids()
    listed = world.delete_list([ids["a"], ids["b"]])
    world.qq.error = ServerError("connection reset")

    out = world.delete(listed["list_id"])
    assert out["ok"] is False and out["done"] == 1 and out["ids"] == [ids["a"]]
    assert out["errors"] == [{"mailbox": "QQ", "ids": [ids["b"]],
                              "error": out["errors"][0]["error"]}]
    assert out["remaining"] == 1
    assert any("delete" in w and listed["list_id"] in w for w in out["warnings"])
    assert a not in world.work.inbox and b in world.qq.inbox
    left = actions.show_delete_list(world.mail, listed["list_id"])
    assert [i["id"] for i in left["items"]] == [ids["b"]] and left["total"] == 1

    world.qq.error = None
    out = world.delete(listed["list_id"])
    assert (out["ok"], out["ids"], out["remaining"]) == (True, [ids["b"]], 0)
    assert b not in world.qq.inbox
    assert world.work.writes() == [("move", (a,), "Trash")]  # not touched again
    assert list(lists_dir(world).iterdir()) == []


def test_delete_skips_what_changed_since_the_list_was_made(world):
    world.deliver("kept")
    world.deliver("archived since")
    world.deliver("gone")
    world.poll()
    ids = world.ids()
    listed = world.delete_list([ids["kept"], ids["archived since"], ids["gone"]])
    world.archive([ids["archived since"]])
    out = world.delete(listed["list_id"])
    assert out["ok"] is False and out["ids"] == [ids["kept"], ids["gone"]]
    (refusal,) = out["refused"]
    assert refusal["id"] == ids["archived since"]
    assert "already moved to Archive" in refusal["reason"]
    assert out["remaining"] == 0 and list(lists_dir(world).iterdir()) == []
    assert len(world.work.folders_["Trash"]) == 2


def test_delete_refuses_a_listed_message_whose_mailbox_was_renumbered(world):
    uid = world.deliver("a")
    world.deliver("b")
    world.poll()
    ids = world.ids()
    listed = world.delete_list([ids["a"], ids["b"]])
    world.work.uidvalidity = 9
    world.poll(2)  # the index learns of the new numbering
    world.work.calls.clear()

    out = world.delete(listed["list_id"])
    assert out["ok"] is False and out["done"] == 0
    assert sorted(r["id"] for r in out["refused"]) == sorted(ids.values())
    assert all("search again" in r["reason"] for r in out["refused"])
    assert world.work.calls == [] and uid in world.work.inbox
    assert list(lists_dir(world).iterdir()) == []  # nothing left that could ever be done


def test_delete_trusts_the_saved_uid_not_the_index_alone(world):
    uid = world.deliver("a")
    world.poll()
    (mid,) = world.ids().values()
    listed = world.delete_list([mid])
    path = lists_dir(world) / f"{listed['list_id']}.json"
    data = json.loads(path.read_text("utf-8"))
    data["items"][0]["uid"] = uid + 1  # a list that does not describe the indexed message
    path.write_text(json.dumps(data), "utf-8")
    out = world.delete(listed["list_id"])
    assert out["done"] == 0 and "search again" in out["refused"][0]["reason"]
    silent(world.work)


def test_delete_with_a_damaged_list_file_fails_cleanly(world):
    world.deliver("a")
    world.poll()
    (mid,) = world.ids().values()
    listed = world.delete_list([mid])
    path = lists_dir(world) / f"{listed['list_id']}.json"
    path.write_text("{not json", "utf-8")
    with pytest.raises(MailError, match="cannot be read"):
        world.delete(listed["list_id"])
    path.write_text(json.dumps({"items": [{"id": "x"}]}), "utf-8")
    with pytest.raises(MailError, match="cannot be read"):
        world.delete(listed["list_id"])
    silent(world.work)


# -- attachment names ------------------------------------------------------------------------


@pytest.mark.parametrize("name, expected", [
    ("offer.pdf", "offer.pdf"),
    ("简历-张三.pdf", "简历-张三.pdf"),
    ("报告 (终稿).docx", "报告 (终稿).docx"),
    ("a/b\\c:d.pdf", "a_b_c_d.pdf"),
    ("../../etc/passwd", "_.._etc_passwd"),
    ("x.pdf:stream", "x.pdf_stream"),
    ("C:\\Windows\\win.ini", "C__Windows_win.ini"),
    ('q"u<e>r|y?*.txt', "q_u_e_r_y__.txt"),
    ("..", "attachment-3"),
    (".", "attachment-3"),
    ("", "attachment-3"),
    ("   ", "attachment-3"),
    ("....", "attachment-3"),
    ("...hidden", "hidden"),
    (".bashrc", "bashrc"),
    ("name. . ", "name"),
    ("  name.pdf  ", "name.pdf"),
    ("CON", "_CON"),
    ("con.txt", "_con.txt"),
    ("NUL.tar.gz", "_NUL.tar.gz"),
    ("Aux .pdf", "_Aux .pdf"),
    ("COM1", "_COM1"),
    ("lpt9.pdf", "_lpt9.pdf"),
    ("COM\u00b9.txt", "_COM\u00b9.txt"),
    ("COM10.txt", "COM10.txt"),
    ("console.txt", "console.txt"),
    ("a\x00b\x1fc\x7f.pdf", "abc.pdf"),
    ("evil\u202egpj.exe", "evilgpj.exe"),
    ("bad\udcffname.pdf", "bad_name.pdf"),
    ("tab\tname.pdf", "tabname.pdf"),
])
def test_safe_filename(name, expected):
    assert actions.safe_filename(name, 3) == expected


def test_safe_filename_caps_the_length_and_keeps_the_extension():
    long_ascii = actions.safe_filename("a" * 400 + ".pdf", 0)
    assert len(long_ascii) <= 150 and long_ascii.endswith(".pdf") and long_ascii.startswith("aaa")
    chinese = actions.safe_filename("长" * 300 + ".pdf", 0)
    assert chinese.endswith(".pdf") and chinese.startswith("长")
    assert len(chinese) <= 150 and len(chinese.encode("utf-8")) <= 200
    for name in (long_ascii, chinese):
        assert name == name.rstrip(". ")
    # an "extension" that is not one (too long) is cut like the rest, not kept whole
    weird = actions.safe_filename("a." + "y" * 300, 0)
    assert len(weird) <= 150


# -- attachments -----------------------------------------------------------------------------


PDF = b"%PDF-1.4 resume"


def attachment_mail(world, name="简历-张三.pdf", data=PDF, *, server=None, **options):
    uid = world.deliver("resume", server=server, attachments=[(name, data, "application/pdf")],
                        **options)
    world.poll()
    return uid, world.ids()["resume"]


def test_download_saves_under_the_mailbox_and_month_with_the_id_in_the_name(world):
    _uid, mid = attachment_mail(world)
    path = world.download(mid)
    assert path == world.mail / "attachments" / WORK / "2026-10" / f"{mid}-简历-张三.pdf"
    assert path.read_bytes() == PDF
    # reading never marks the message seen: nothing was written to the server
    silent(world.work)
    assert world.work.flags == {} and world.connects == ["工作"]
    assert world.work.closed


def test_download_returns_a_path_with_no_unsafe_part_for_a_hostile_name(world):
    _, mid = attachment_mail(world, "x.pdf:stream")
    path = world.download(mid)
    assert path.name == f"{mid}-x.pdf_stream"
    assert path.parent == world.mail / "attachments" / WORK / "2026-10"
    assert path.read_bytes() == PDF


def test_download_month_is_the_utc_month_of_the_date_header(world):
    late = datetime(2026, 3, 1, 0, 30, tzinfo=timezone(timedelta(hours=8)))  # Feb 28, 16:30 UTC
    _, mid = attachment_mail(world, date=late)
    assert world.download(mid).parent.name == "2026-02"


def test_download_of_an_address_that_needs_a_hashed_directory_name(hub_dir):
    odd = "o'brien+x@corp.example"  # an apostrophe: valid, but not safe in a file name
    fake = FakeServer()
    add(hub_dir, fake, "工作", odd)
    fake.deliver(raw("resume", to=odd, attachments=[("a.pdf", PDF)]))
    poll.poll(hub_dir, mail(hub_dir), connect=fake_connect(fake), now=NOW)
    with Store.open(mail(hub_dir)) as store:
        (mid,) = (m["id"] for m in store.search("")["items"])
    path = actions.download_attachment(hub_dir, mail(hub_dir), mid, 0, connect=fake_connect(fake))
    assert path.parent.parent.name == accounts.address_file_name(odd)
    assert "~" in path.parent.parent.name and path.read_bytes() == PDF


def test_download_twice_reuses_the_file(world):
    _, mid = attachment_mail(world)
    first = world.download(mid)
    stamp = first.stat().st_mtime_ns
    again = world.download(mid)
    assert again == first and first.stat().st_mtime_ns == stamp
    assert sorted(p.name for p in first.parent.iterdir()) == [first.name]


def test_two_attachments_with_one_name_get_two_files(world):
    uid = world.deliver(
        "two", attachments=[("scan.pdf", b"first one"), ("scan.pdf", b"second one")])
    world.poll()
    mid = world.ids()["two"]
    one, two = world.download(mid, 0), world.download(mid, 1)
    assert one.name == f"{mid}-scan.pdf" and two.name == f"{mid}-scan (2).pdf"
    assert (one.read_bytes(), two.read_bytes()) == (b"first one", b"second one")
    # asking again finds each file again, whatever the order
    assert world.download(mid, 1) == two and world.download(mid, 0) == one
    assert len(list(one.parent.iterdir())) == 2
    assert uid in world.work.inbox


def test_download_never_overwrites_a_different_file(world):
    _, mid = attachment_mail(world, "offer.pdf")
    folder = world.mail / "attachments" / WORK / "2026-10"
    folder.mkdir(parents=True)
    (folder / f"{mid}-offer.pdf").write_bytes(b"something else")
    path = world.download(mid)
    assert path.name == f"{mid}-offer (2).pdf" and path.read_bytes() == PDF
    assert (folder / f"{mid}-offer.pdf").read_bytes() == b"something else"
    assert world.download(mid) == path  # and it finds its own copy next time


def test_download_a_name_of_only_dots_gets_a_name_from_the_index(world):
    uid = world.deliver("dots", attachments=[("..", b"x")])
    world.poll()
    mid = world.ids()["dots"]
    path = world.download(mid)
    assert path.name == f"{mid}-attachment-0" and path.parent.parent.parent.name == "attachments"
    assert uid in world.work.inbox


def test_download_a_bad_index_is_an_error_without_a_connection(world):
    _, mid = attachment_mail(world)
    with pytest.raises(MailError, match="has 1 attachment"):
        world.download(mid, 1)
    world.deliver("plain")
    world.poll(2)
    with pytest.raises(MailError, match="has no attachments"):
        world.download(world.ids()["plain"], 0)
    assert world.connects == []


def test_download_of_an_unknown_message(world):
    with pytest.raises(MailError, match="no message with id 7"):
        world.download(7)


def test_download_refuses_a_stale_message(world):
    _, mid = attachment_mail(world)
    world.work.uidvalidity = 3
    with pytest.raises(MailError, match="search again"):
        world.download(mid)  # the server says so
    world.poll(2)
    world.connects.clear()
    with pytest.raises(MailError, match="search again"):
        world.download(mid)  # the index knows now
    assert world.connects == []


def test_download_of_a_moved_message_says_it_left_inbox(world):
    _, mid = attachment_mail(world)
    world.archive([mid])
    with pytest.raises(MailError, match="already moved to Archive"):
        world.download(mid)


def test_download_when_the_message_is_gone_from_the_server(world):
    uid, mid = attachment_mail(world)
    del world.work.inbox[uid]  # deleted from a phone
    with pytest.raises(MailError, match="no longer in INBOX"):
        world.download(mid)
    silent(world.work)


def test_download_error_never_shows_the_password(world):
    _, mid = attachment_mail(world)
    world.work.error = ServerError(f"NO fetch for {PASSWORD}")
    with pytest.raises(MailError) as caught:
        world.download(mid)
    assert PASSWORD not in str(caught.value) and "[password]" in str(caught.value)


def test_download_into_a_blocked_directory_is_a_mail_error(world):
    _, mid = attachment_mail(world)
    (world.mail / "attachments").write_text("a file where the folder should be", "utf-8")
    with pytest.raises(MailError, match="cannot save"):
        world.download(mid)


# -- the command line ------------------------------------------------------------------------


@pytest.fixture
def mk(world, monkeypatch, capsys):
    monkeypatch.setattr(server, "connect", fake_connect(world.servers))

    def run_mk(*args):
        argv = ["--mail-dir", str(world.mail), "--hub", str(world.hub), *args]
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        status = cli.main(argv)
        out, err = capsys.readouterr()
        return status, json.loads(out) if out.strip() else out, err

    run_mk.world = world
    return run_mk


def test_cli_flag_archive_delete_and_attachment_end_to_end(world, mk):
    a = world.deliver("a")
    b = world.deliver("b")
    c = world.deliver("c")
    uid = world.deliver("resume", attachments=[("简历-张三.pdf", PDF)])
    world.poll()
    ids = world.ids()

    status, out, err = mk("flag", str(ids["a"]), str(ids["b"]), "--seen")
    assert (status, err) == (0, "") and out["done"] == 2 and out["seen"] is True
    assert world.work.flags == {a: {"\\Seen"}, b: {"\\Seen"}}
    status, out, _ = mk("flag", str(ids["a"]), "--unseen")
    assert (status, out["seen"], out["done"]) == (0, False, 1)

    status, out, _ = mk("archive", str(ids["c"]))
    assert (status, out["mailboxes"][0]["folder"]) == (0, "Archive")
    assert c not in world.work.inbox
    status, out, _ = mk("archive", str(ids["c"]))  # again: refused, and the exit status says so
    assert status == 1 and out["ok"] is False and "already moved" in out["refused"][0]["reason"]

    status, out, err = mk("delete-list", str(ids["a"]), str(ids["b"]))
    assert (status, err) == (0, "") and out["total"] == 2
    list_id = out["list_id"]
    status, out, _ = mk("delete-list", "--show", list_id)
    assert (status, out["total"], out["next_page"]) == (0, 2, None)
    status, out, _ = mk("delete", list_id)
    assert (status, out["done"], out["remaining"]) == (0, 2, 0)
    assert a not in world.work.inbox and b not in world.work.inbox
    status, out, err = mk("delete", list_id)
    assert status == 1 and out == "" and err.startswith("mk: ") and "no delete list" in err

    status, out, err = mk("attachment", "get", str(ids["resume"]), "0")
    assert (status, err) == (0, "") and set(out) == {"path"}
    expected = world.mail / "attachments" / WORK / "2026-10" / f"{ids['resume']}-简历-张三.pdf"
    assert Path(out["path"]) == expected and expected.read_bytes() == PDF
    assert "\\" not in out["path"]  # always a forward-slash path
    assert uid in world.work.inbox


def test_cli_bg_attachment_is_the_read_only_twin(world, mk):
    uid = world.deliver("resume", attachments=[("offer.pdf", PDF)])
    world.poll()
    mid = world.ids()["resume"]
    status, out, err = mk("bg", "attachment", str(mid), "0")
    assert (status, err) == (0, "") and set(out) == {"path"}
    assert out["path"].endswith(f"/{mid}-offer.pdf") and "\\" not in out["path"]
    assert Path(out["path"]).read_bytes() == PDF
    silent(world.work)  # EXAMINE and BODY.PEEK only: not even \Seen
    assert world.work.flags == {} and uid in world.work.inbox
    assert "set_seen" not in world.work.names() and "move" not in world.work.names()


def test_cli_write_commands_are_not_in_the_bg_group():
    parser = cli.build_parser()
    top = next(a for a in parser._actions if a.dest == "command")
    bg = next(a for a in top.choices["bg"]._actions if a.dest == "action")
    assert "attachment" in bg.choices
    for name in ("flag", "archive", "delete", "delete-list"):
        assert name in top.choices and name not in bg.choices
    attachment = next(a for a in top.choices["attachment"]._actions if a.dest == "action")
    assert "get" in attachment.choices


def test_cli_bg_attachment_takes_only_numbers(world, mk):
    for argv in (["bg", "attachment", "x", "0"], ["bg", "attachment", "1", "-1"],
                 ["bg", "attachment", "1"], ["bg", "attachment", "1", "0", "--create"]):
        status, out, err = mk(*argv)
        assert status == 1 and out == "" and "mk: " in err


@pytest.mark.parametrize("argv, message", [
    (["flag", "1"], "--seen"),
    (["flag", "--seen"], "ID"),
    (["flag", "1", "--seen", "--unseen"], "not allowed with"),
    (["flag", "x", "--seen"], "not a number"),
    (["archive"], "ID"),
    (["archive", "1", "--page", "2"], "unrecognized"),
    (["delete-list"], "give message ids"),
    (["delete-list", "1", "--show", "abcdefgh"], "either"),
    (["delete-list", "1", "--page", "2"], "--show"),
    (["delete"], "required"),
    (["delete", "../x"], "not a delete list id"),
    (["delete", "abcdefgh"], "no delete list"),
    (["attachment", "get", "1"], "required"),
    (["attachment", "get", "1", "x"], "not a number"),
])
def test_cli_usage_errors_are_mk_errors(world, mk, argv, message):
    status, out, err = mk(*argv)
    assert status == 1 and out == ""
    assert "mk: " in err and message in err
    assert "Traceback" not in err


def test_cli_a_message_that_is_gone_exits_1_with_json(world, mk):
    status, out, err = mk("flag", "99", "--seen")
    assert (status, err) == (1, "")
    assert out["ok"] is False and out["refused"][0]["reason"] == "no message with id 99"


def test_cli_never_prints_the_password(world, mk):
    world.deliver("a")
    world.poll()
    (mid,) = world.ids().values()
    world.work.error = ServerError(f"NO {PASSWORD}")
    for argv in (["flag", str(mid), "--seen"], ["archive", str(mid)]):
        status, out, err = mk(*argv)
        assert status == 1 and PASSWORD not in json.dumps(out) + err
    list_id = mk("delete-list", str(mid))[1]["list_id"]
    status, out, err = mk("delete", list_id)
    assert status == 1 and PASSWORD not in json.dumps(out) + err

import io
import json
import os
import shutil
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import ANY

import pytest
from mailfakes import FakeServer, fake_connect, make_raw
from mailkit import accounts, cli, poll, server
from mailkit.accounts import read_password, secret_path
from mailkit.config import MailError, load, read_text, write_text
from mailkit.server import LoginError
from mailkit.store import Store

ROOT = Path(__file__).parents[1]
SEED_MAIL = ROOT / "mail" / "seed" / "mail"
NOW = datetime(2026, 10, 14, 4, 0, tzinfo=UTC)
WORK = "me@corp.example"


@pytest.fixture
def hub_dir(tmp_path):
    hub = tmp_path / "hub"
    shutil.copytree(SEED_MAIL, hub / "mail")
    (hub / "secrets").mkdir()
    return hub


def add(hub, name="工作", address=WORK, password="pw", *, server=None, provider="feishu",
        **options):
    return accounts.add(
        hub, hub / "mail", name, address, password, provider=provider,
        connect=fake_connect(server or FakeServer()), now=NOW, **options)


def state(hub, address=WORK):
    with Store.open(hub / "mail") as store:
        return store.state(address)


# -- add -------------------------------------------------------------------------------------


def test_add_tests_both_logins_then_writes(hub_dir):
    fake = FakeServer()
    r = accounts.add(hub_dir, hub_dir / "mail", "工作", "me@corp.example", "pw",
                     provider="feishu", connect=fake_connect(fake), now=NOW)
    assert r == {"ok": True, "mailbox": "工作", "address": "me@corp.example",
                 "provider": "feishu"}
    box = load(hub_dir / "mail").mailboxes["工作"]
    assert (box.address, box.provider) == ("me@corp.example", "feishu")
    assert (box.imap, box.smtp) == ("imap.feishu.cn:993", "smtp.feishu.cn:465")
    assert datetime.fromisoformat(box.added) == NOW  # with the local offset
    assert datetime.fromisoformat(box.added).tzinfo is not None
    assert read_password(hub_dir, "me@corp.example") == "pw"
    assert "pw" not in (hub_dir / "mail" / "config.toml").read_text("utf-8")
    assert "pw" not in json.dumps(r, ensure_ascii=False)
    assert fake.logins == [("me@corp.example", "pw")]
    assert fake.names()[0] == "check_login" and fake.closed
    assert fake.writes() == []


@pytest.mark.parametrize("step", ["connect", "imap-login", "smtp-login"])
def test_add_failure_writes_nothing(hub_dir, step):
    before = read_text(hub_dir / "mail")
    r = add(hub_dir, server=FakeServer(fail_login=step))
    assert r == {"ok": False, "step": step, "message": ANY, "hint": ANY}
    assert r["message"] and r["hint"]
    assert load(hub_dir / "mail").mailboxes == {}
    assert read_text(hub_dir / "mail") == before
    assert not (hub_dir / "secrets" / "mail").exists()
    assert state(hub_dir) is None


def test_failure_hints_name_the_step_and_the_guide(hub_dir):
    hints = {step: add(hub_dir, server=FakeServer(fail_login=step))["hint"]
             for step in ("connect", "imap-login", "smtp-login")}
    assert "network" in hints["connect"]
    assert "feishu.md" in hints["imap-login"]
    assert "SMTP" in hints["smtp-login"]
    assert len(set(hints.values())) == 3


def test_a_server_message_with_the_password_is_scrubbed(hub_dir):
    class Echo(FakeServer):
        def check_login(self):
            raise LoginError("imap-login", f"no such user or password {self.given_password}")

    r = add(hub_dir, password="s3cret-Pw", server=Echo())
    assert r["ok"] is False
    assert "s3cret-Pw" not in json.dumps(r, ensure_ascii=False)


def test_add_sets_baseline_so_old_mail_is_not_new(hub_dir):
    fake = FakeServer(uidvalidity=777)
    fake.deliver(make_raw("old", "a@x.example", WORK, "old"), received=NOW - timedelta(days=200))
    fake.deliver(make_raw("month", "a@x.example", WORK, "m"), received=NOW - timedelta(days=30))
    fake.deliver(make_raw("today", "a@x.example", WORK, "t"), received=NOW)
    assert add(hub_dir, server=fake)["ok"]
    # backfill runs downward from just above the newest old mail
    assert state(hub_dir) == {"uidvalidity": 777, "last_uid": 3, "backfill_before": 4}


def test_add_backfill_starts_above_last_uid_even_with_gaps(hub_dir):
    fake = FakeServer({2: make_raw("month", "a@x.example", WORK, "m")},
                      received={2: NOW - timedelta(days=30)})
    fake.uidnext = 7  # mail 3..6 came and went
    assert add(hub_dir, server=fake)["ok"]
    assert state(hub_dir) == {"uidvalidity": 1, "last_uid": 6, "backfill_before": 7}


def test_mail_that_arrives_during_the_add_is_not_backfill(hub_dir):
    class Late(FakeServer):
        def uids_since(self, day):
            uids = super().uids_since(day)
            self.deliver(make_raw("late", "a@x.example", WORK, "l"), received=NOW)
            return uids + [self.uidnext - 1]

    fake = Late({1: make_raw("old", "a@x.example", WORK, "o")},
                received={1: NOW - timedelta(days=200)})
    assert add(hub_dir, server=fake)["ok"]
    assert state(hub_dir) == {"uidvalidity": 1, "last_uid": 1, "backfill_before": None}


def test_add_baseline_with_gaps_and_nothing_recent(hub_dir):
    fake = FakeServer({5: make_raw("old", "a@x.example", WORK, "x")},
                      received={5: NOW - timedelta(days=400)})
    fake.uidnext = 9  # mail 6..8 came and went
    assert add(hub_dir, server=fake)["ok"]
    assert state(hub_dir) == {"uidvalidity": 1, "last_uid": 8, "backfill_before": None}


def test_add_empty_inbox(hub_dir):
    assert add(hub_dir, server=FakeServer())["ok"]
    assert state(hub_dir) == {"uidvalidity": 1, "last_uid": 0, "backfill_before": None}


def test_add_starts_the_index_of_an_address_afresh(hub_dir):
    with Store.open(hub_dir / "mail") as store:
        store.set_baseline(WORK, 1, 500, 3)
        store.problem(WORK, "login", "old problem", NOW)
    assert add(hub_dir, server=FakeServer(uidvalidity=2))["ok"]
    assert state(hub_dir) == {"uidvalidity": 2, "last_uid": 0, "backfill_before": None}
    with Store.open(hub_dir / "mail") as store:
        assert store.problems(unreported_only=False) == []


def test_unsupported_and_duplicate(hub_dir):
    fake = FakeServer()
    with pytest.raises(MailError, match="not supported"):
        accounts.add(hub_dir, hub_dir / "mail", "Outlook", "me@outlook.com", "pw",
                     connect=fake_connect(fake), now=NOW)
    assert fake.calls == []
    assert add(hub_dir, server=fake)["ok"]
    with pytest.raises(MailError, match="already a mailbox named"):
        add(hub_dir, "工作", "other@corp.example", server=fake)
    with pytest.raises(MailError, match="already added"):
        add(hub_dir, "Work 2", "ME@Corp.Example", server=fake)
    assert fake.names().count("check_login") == 1  # refused before any login
    assert list(load(hub_dir / "mail").mailboxes) == ["工作"]


def test_add_detects_the_provider(hub_dir):
    r = accounts.add(hub_dir, hub_dir / "mail", "QQ", "me@qq.com", "pw",
                     connect=fake_connect(FakeServer()), now=NOW)
    assert r["provider"] == "qq"
    r = accounts.add(hub_dir, hub_dir / "mail", "工作", "me@corp.example", "pw",
                     connect=fake_connect(FakeServer()), now=NOW,
                     mx=lambda domain: ["mx1.feishu.cn."])
    assert r["provider"] == "feishu"
    # a domain the app cannot place: no guess, the error says to ask and pass --provider
    with pytest.raises(MailError, match="ask the user.*--provider"):
        accounts.add(hub_dir, hub_dir / "mail", "x", "me@nowhere.example", "pw",
                     connect=fake_connect(FakeServer()), now=NOW, mx=lambda domain: [])
    r = accounts.add(hub_dir, hub_dir / "mail", "x", "me@nowhere.example", "pw",
                     provider="feishu", connect=fake_connect(FakeServer()), now=NOW,
                     mx=lambda domain: pytest.fail("the user named the provider"))
    assert (r["ok"], r["provider"]) == (True, "feishu")


def test_add_unknown_provider_key(hub_dir):
    with pytest.raises(MailError, match="163, 126"):
        add(hub_dir, provider="outlook")


@pytest.mark.parametrize("name,address", [
    ('a"b', WORK), (" 工作", WORK), ("", WORK), ("工作", "not-an-address"),
    ("工作", "me@corp.example\nx"), ("工作", ""),
])
def test_add_rejects_bad_names_and_addresses(hub_dir, name, address):
    fake = FakeServer()
    with pytest.raises(MailError):
        add(hub_dir, name, address, server=fake)
    assert fake.calls == []


@pytest.mark.parametrize("password", ["", "pw\nmore", "tab\there", "密码abc", "\x00pw"])
def test_add_rejects_unusable_passwords_without_echoing_them(hub_dir, password):
    fake = FakeServer()
    with pytest.raises(MailError) as caught:
        add(hub_dir, password=password, server=fake)
    if password:
        assert password not in str(caught.value)
    assert fake.calls == []


def test_add_keeps_spaces_inside_a_password(hub_dir):
    assert add(hub_dir, password="abcd efgh ijkl mnop")["ok"]
    assert read_password(hub_dir, WORK) == "abcd efgh ijkl mnop"


def test_add_rolls_back_when_the_index_fails(hub_dir, monkeypatch):
    before = read_text(hub_dir / "mail")

    def broken(self, *args):
        raise MailError("the mail index is busy")

    monkeypatch.setattr(Store, "set_baseline", broken)
    with pytest.raises(MailError, match="busy"):
        add(hub_dir)
    assert read_text(hub_dir / "mail") == before
    assert not (hub_dir / "secrets" / "mail").exists()


def test_add_rolls_back_when_the_config_cannot_be_written(hub_dir, monkeypatch):
    def broken(mail_dir, text):
        raise MailError("cannot write config.toml: disk full")

    monkeypatch.setattr(accounts.config, "write_text", broken)
    with pytest.raises(MailError, match="disk full"):
        add(hub_dir)
    assert not (hub_dir / "secrets" / "mail").exists()
    assert load(hub_dir / "mail").mailboxes == {}


def test_add_rollback_keeps_an_existing_secrets_folder_and_restores_an_old_file(
        hub_dir, monkeypatch):
    folder = hub_dir / "secrets" / "mail"
    folder.mkdir()
    (folder / "other@qq.com.toml").write_text('password = "x"\n', "utf-8")
    old = secret_path(hub_dir, WORK)
    old.write_bytes(b'password = "left over"\n')

    def broken(self, *args):
        raise MailError("busy")

    monkeypatch.setattr(Store, "set_baseline", broken)
    with pytest.raises(MailError):
        add(hub_dir)
    assert old.read_bytes() == b'password = "left over"\n'
    assert (folder / "other@qq.com.toml").exists()


def test_add_rollback_leaves_hand_edits_made_meanwhile(hub_dir, monkeypatch):
    def broken(self, *args):
        # the user saves an edit of config.toml while the add is running
        text = read_text(hub_dir / "mail")
        write_text(hub_dir / "mail", text.replace("notes = []", 'notes = ["mine"]', 1))
        raise MailError("busy")

    monkeypatch.setattr(Store, "set_baseline", broken)
    with pytest.raises(MailError):
        add(hub_dir)
    settings = load(hub_dir / "mail")
    assert settings.mailboxes == {} and settings.notes == ["mine"]


def test_reading_the_inbox_fails_after_login(hub_dir):
    fake = FakeServer(error=server.ServerError("connection reset"))
    with pytest.raises(MailError, match="connection reset"):
        add(hub_dir, server=fake)
    assert load(hub_dir / "mail").mailboxes == {}
    assert not (hub_dir / "secrets" / "mail").exists()
    assert fake.closed


# -- secrets ---------------------------------------------------------------------------------


def test_secret_file_name_and_content(hub_dir):
    assert add(hub_dir, address="me+x@corp.example", password='p"w\\d')["ok"]
    path = hub_dir / "secrets" / "mail" / "me+x@corp.example.toml"
    assert secret_path(hub_dir, "me+x@corp.example") == path
    assert path.exists()
    assert read_password(hub_dir, "me+x@corp.example") == 'p"w\\d'
    assert read_password(hub_dir, "Me+X@Corp.Example") == 'p"w\\d'
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no temp files left
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("address", [
    "张三@corp.example", "a/b@corp.example", "a\\b@corp.example", "..@corp.example",
    "a:b@corp.example", "CON@corp.example", "x" * 300 + "@corp.example",
])
def test_secret_paths_stay_in_the_folder(hub_dir, address):
    path = secret_path(hub_dir, address)
    assert path.parent == hub_dir / "secrets" / "mail"
    assert path.name.endswith(".toml") and len(path.name) <= 220
    assert path.name not in (".toml", "..toml")
    assert set(path.stem) <= set("abcdefghijklmnopqrstuvwxyz0123456789._+-@~")


def test_secret_names_of_different_addresses_differ():
    names = {accounts.address_file_name(a) for a in (
        "a/b@x.example", "a\\b@x.example", "a_b@x.example", "a:b@x.example",
        "张三@x.example", "李四@x.example")}
    assert len(names) == 6
    assert accounts.address_file_name("A_B@X.example") == "a_b@x.example"


@pytest.mark.parametrize("text", ["", ".", "..", "con", "NUL", "com1", "lpt9", "aux.txt"])
def test_unusable_file_names_are_refused(text):
    with pytest.raises(MailError):
        accounts.address_file_name(text)


def test_read_password_errors_do_not_show_the_file(hub_dir):
    with pytest.raises(MailError, match="no password stored for me@corp.example"):
        read_password(hub_dir, WORK)
    path = secret_path(hub_dir, WORK)
    path.parent.mkdir(parents=True)
    path.write_text('password = "abc\n', "utf-8")  # broken TOML
    with pytest.raises(MailError) as caught:
        read_password(hub_dir, WORK)
    assert "abc" not in str(caught.value)
    path.write_text("password = 3\n", "utf-8")
    with pytest.raises(MailError, match="password"):
        read_password(hub_dir, WORK)


# -- test, edit, remove, list ----------------------------------------------------------------


def test_test_logs_in_with_the_stored_password(hub_dir):
    fake = FakeServer(password="pw")
    add(hub_dir, server=fake)
    r = accounts.test(hub_dir, hub_dir / "mail", "工作", connect=fake_connect(fake))
    assert r == {"ok": True, "mailbox": "工作", "address": WORK}
    assert fake.logins[-1] == (WORK, "pw") and fake.closed
    fake.password = "rotated"
    r = accounts.test(hub_dir, hub_dir / "mail", "工作", connect=fake_connect(fake))
    assert r == {"ok": False, "step": "imap-login", "message": ANY, "hint": ANY}
    with pytest.raises(MailError, match="no mailbox named"):
        accounts.test(hub_dir, hub_dir / "mail", "nope", connect=fake_connect(fake))


def test_edit_password_relogs_and_rename(hub_dir):
    fake = FakeServer(password="pw")
    add(hub_dir, server=fake)
    mail = hub_dir / "mail"
    fake.password = "new-pw"
    r = accounts.edit(hub_dir, mail, "工作", password="wrong", connect=fake_connect(fake))
    assert r == {"ok": False, "step": "imap-login", "message": ANY, "hint": ANY}
    assert read_password(hub_dir, WORK) == "pw"

    r = accounts.edit(hub_dir, mail, "工作", password="new-pw", new_name="Feishu",
                      paused=True, connect=fake_connect(fake))
    assert r == {"ok": True, "mailbox": "Feishu", "address": WORK,
                 "changed": ["password", "name", "paused"]}
    assert "new-pw" not in json.dumps(r)
    assert fake.logins[-1] == (WORK, "new-pw")
    assert read_password(hub_dir, WORK) == "new-pw"
    boxes = load(mail).mailboxes
    assert list(boxes) == ["Feishu"] and boxes["Feishu"].paused is True
    assert boxes["Feishu"].address == WORK

    r = accounts.edit(hub_dir, mail, "Feishu", paused=False, connect=fake_connect(fake))
    assert r["changed"] == ["paused"]
    assert load(mail).mailboxes["Feishu"].paused is False
    assert "paused" not in read_text(mail)  # the default is not written


def test_edit_refusals(hub_dir):
    fake = FakeServer()
    add(hub_dir, server=fake)
    add(hub_dir, "QQ", "me@qq.com", provider="qq", server=fake)
    mail = hub_dir / "mail"
    with pytest.raises(MailError, match="already a mailbox named"):
        accounts.edit(hub_dir, mail, "工作", new_name="QQ", connect=fake_connect(fake))
    with pytest.raises(MailError, match="nothing to change"):
        accounts.edit(hub_dir, mail, "工作", connect=fake_connect(fake))
    with pytest.raises(MailError, match="no mailbox named"):
        accounts.edit(hub_dir, mail, "nope", paused=True, connect=fake_connect(fake))
    with pytest.raises(MailError):
        accounts.edit(hub_dir, mail, "工作", new_name='bad"name', connect=fake_connect(fake))
    before = len(fake.calls)
    with pytest.raises(MailError):
        accounts.edit(hub_dir, mail, "工作", password="密码", connect=fake_connect(fake))
    assert len(fake.calls) == before


def test_rename_keeps_the_rest_of_the_section(hub_dir):
    add(hub_dir)
    mail = hub_dir / "mail"
    text = read_text(mail).replace('[mailboxes."工作"]', '[mailboxes."工作"]  # my work mail')
    text = text.replace('provider = "feishu"', 'provider = "feishu"\nnotes = ["urgent ones"]')
    write_text(mail, text)
    accounts.edit(hub_dir, mail, "工作", new_name="Work", connect=fake_connect(FakeServer()))
    after = read_text(mail)
    assert '[mailboxes."Work"]  # my work mail' in after
    assert load(mail).mailboxes["Work"].notes == ["urgent ones"]


def test_edit_rolls_back_the_password_when_the_config_fails(hub_dir, monkeypatch):
    fake = FakeServer()
    add(hub_dir, server=fake)

    def broken(mail_dir, text):
        raise MailError("cannot write config.toml")

    monkeypatch.setattr(accounts.config, "write_text", broken)
    with pytest.raises(MailError, match="cannot write"):
        accounts.edit(hub_dir, hub_dir / "mail", "工作", password="new", new_name="X",
                      connect=fake_connect(fake))
    assert read_password(hub_dir, WORK) == "pw"


def test_remove_deletes_secret_rules_and_index_keeps_attachments(hub_dir):
    mail = hub_dir / "mail"
    add(hub_dir, address="me+x@corp.example")
    add(hub_dir, "QQ", "me@qq.com", provider="qq")
    attachment = mail / "attachments" / "me+x@corp.example" / "2026-10" / "1-a.pdf"
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b"%PDF")
    with Store.open(mail) as store:
        store.problem("me+x@corp.example", "login", "password expired", NOW)

    r = accounts.remove(hub_dir, mail, "工作")
    assert r == {"ok": True, "mailbox": "工作", "address": "me+x@corp.example",
                 "password_deleted": True}
    assert attachment.read_bytes() == b"%PDF"
    assert not secret_path(hub_dir, "me+x@corp.example").exists()
    assert list(load(mail).mailboxes) == ["QQ"]
    assert '"工作"' not in read_text(mail)
    with Store.open(mail) as store:
        assert store.state("me+x@corp.example") is None
        assert store.problems(unreported_only=False) == []
        assert store.state("me@qq.com") is not None
    assert read_password(hub_dir, "me@qq.com") == "pw"
    with pytest.raises(MailError, match="no mailbox named"):
        accounts.remove(hub_dir, mail, "工作")


def test_remove_fails_whole_when_the_index_is_busy(hub_dir, monkeypatch):
    add(hub_dir)

    def busy(self, address):
        raise MailError("the mail index is busy")

    monkeypatch.setattr(Store, "forget", busy)
    with pytest.raises(MailError, match="busy"):
        accounts.remove(hub_dir, hub_dir / "mail", "工作")
    assert list(load(hub_dir / "mail").mailboxes) == ["工作"]
    assert read_password(hub_dir, WORK) == "pw"


def test_remove_waits_for_a_running_poll(hub_dir):
    """A poll that read config.toml before the removal would write the removed mailbox's mail
    and problems back into the index: `remove` waits until the poll is done."""
    add(hub_dir)
    mail = hub_dir / "mail"
    locked, done = threading.Event(), []

    def a_poll():
        with poll.poll_lock(mail) as held:
            locked.set()
            time.sleep(0.6)
            done.append(held)

    holder = threading.Thread(target=a_poll)
    holder.start()
    try:
        assert locked.wait(10)
        r = accounts.remove(hub_dir, mail, "工作")
        assert done == [True]  # the poll had finished before anything was removed
    finally:
        holder.join(10)
    assert r["ok"] and list(load(mail).mailboxes) == []


def test_remove_gives_up_when_the_poll_takes_too_long(hub_dir, monkeypatch):
    add(hub_dir)
    mail = hub_dir / "mail"
    monkeypatch.setattr(accounts, "REMOVE_WAIT_SECONDS", 0.3)
    with poll.poll_lock(mail) as held:
        assert held
        with pytest.raises(MailError, match="check for new mail is running.*nothing was removed"):
            accounts.remove(hub_dir, mail, "工作")
    assert list(load(mail).mailboxes) == ["工作"]
    assert read_password(hub_dir, WORK) == "pw"
    assert state(hub_dir) is not None
    with pytest.raises(MailError, match="no mailbox named"):  # asked before any wait
        accounts.remove(hub_dir, mail, "QQ")


def test_remove_keeps_what_a_second_section_of_the_address_needs(hub_dir):
    add(hub_dir)
    mail = hub_dir / "mail"
    box = load(mail).mailboxes["工作"]
    box.name = "工作 copy"
    write_text(mail, accounts.config.add_mailbox_text(read_text(mail), box))
    r = accounts.remove(hub_dir, mail, "工作")
    assert r["password_deleted"] is False
    assert read_password(hub_dir, WORK) == "pw"
    assert state(hub_dir) is not None
    assert list(load(mail).mailboxes) == ["工作 copy"]


def test_remove_without_a_stored_password(hub_dir):
    add(hub_dir)
    secret_path(hub_dir, WORK).unlink()
    r = accounts.remove(hub_dir, hub_dir / "mail", "工作")
    assert r["ok"] and r["password_deleted"] is False


def test_list_pages(hub_dir):
    for i in range(12):
        add(hub_dir, f"box {i:02}", f"me{i}@qq.com", provider="qq")
    accounts.edit(hub_dir, hub_dir / "mail", "box 03", paused=True,
                  connect=fake_connect(FakeServer()))
    with Store.open(hub_dir / "mail") as store:
        store.resolve("me1@qq.com", NOW)
    first = accounts.list_(hub_dir, hub_dir / "mail")
    assert first["total"] == 12 and first["next_page"] == 2 and len(first["items"]) == 10
    assert first["items"][1] == {"name": "box 01", "address": "me1@qq.com", "provider": "qq",
                                 "paused": False, "last_ok": "2026-10-14T04:00:00+00:00"}
    assert first["items"][3]["paused"] is True and first["items"][3]["last_ok"] is None
    second = accounts.list_(hub_dir, hub_dir / "mail", page=2)
    assert [i["name"] for i in second["items"]] == ["box 10", "box 11"]
    assert second["next_page"] is None and second["total"] == 12
    with pytest.raises(MailError):
        accounts.list_(hub_dir, hub_dir / "mail", page=0)


def test_list_empty(hub_dir):
    assert accounts.list_(hub_dir, hub_dir / "mail") == {
        "items": [], "total": 0, "next_page": None}


# -- the command line ------------------------------------------------------------------------


@pytest.fixture
def mk(hub_dir, monkeypatch, capsys):
    """mk(*args, stdin=None) -> (exit status, stdout JSON or text, stderr)."""
    fake = FakeServer()
    monkeypatch.setattr(server, "connect", fake_connect(fake))

    def run(*args, stdin=None):
        if stdin is not None:
            monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        argv = ["--mail-dir", str(hub_dir / "mail"), "--hub", str(hub_dir), *args]
        status = cli.main(argv)
        out, err = capsys.readouterr()
        try:
            out = json.loads(out)
        except ValueError:
            pass
        return status, out, err

    run.fake = fake
    return run


def test_cli_add_reads_password_from_stdin(hub_dir, mk):
    status, out, err = mk("account", "add", "工作", "--address", WORK, "--provider", "feishu",
                          stdin="pw\n")
    assert (status, err) == (0, "")
    assert out == {"ok": True, "mailbox": "工作", "address": WORK, "provider": "feishu"}
    assert read_password(hub_dir, WORK) == "pw"
    assert mk.fake.logins == [(WORK, "pw")]


def test_cli_add_failure_prints_the_step_and_exits_1(hub_dir, mk):
    mk.fake.fail_login = "smtp-login"
    status, out, err = mk("account", "add", "工作", "--address", WORK, "--provider", "feishu",
                          stdin="pw\r\n")
    assert status == 1 and err == ""
    assert out["ok"] is False and out["step"] == "smtp-login"
    assert "pw" not in json.dumps(out)


def test_cli_add_without_a_password(hub_dir, mk):
    status, out, err = mk("account", "add", "工作", "--address", WORK, stdin="\n")
    assert status == 1 and out == "" and err.startswith("mk: ") and "password" in err


def test_cli_account_commands(hub_dir, mk):
    mk("account", "add", "工作", "--address", WORK, "--provider", "feishu", stdin="pw\n")
    assert mk("account", "test", "工作")[1] == {"ok": True, "mailbox": "工作", "address": WORK}
    status, out, _ = mk("account", "edit", "工作", "--password-stdin", "--rename", "Work",
                        "--pause", stdin="new\n")
    assert status == 0 and out["changed"] == ["password", "name", "paused"]
    assert read_password(hub_dir, WORK) == "new"
    status, out, _ = mk("account", "edit", "Work", "--resume")
    assert out["changed"] == ["paused"]
    status, out, _ = mk("account", "list")
    assert out["total"] == 1 and out["items"][0]["name"] == "Work"
    status, out, _ = mk("account", "list", "--page", "2")
    assert out["items"] == [] and out["total"] == 1
    status, out, _ = mk("account", "remove", "Work")
    assert status == 0 and out["mailbox"] == "Work"
    status, _, err = mk("account", "remove", "Work")
    assert status == 1 and "no mailbox named" in err


def test_cli_detect(mk, monkeypatch):
    status, out, _ = mk("account", "detect", "me@163.com")
    assert status == 0
    assert out == {"address": "me@163.com", "provider": "163", "supported": True,
                   "status": "supported", "reason": "163.com is a NetEase 163 Mail domain",
                   "name": "NetEase 163 Mail", "guide": "skills/mail/providers/netease.md"}
    status, out, _ = mk("account", "detect", "me@outlook.com")
    assert (out["supported"], out["status"]) == (False, "unsupported") and "guide" not in out
    monkeypatch.setattr(accounts.providers, "lookup_mx", lambda domain: [])
    status, out, _ = mk("account", "detect", "me@gateway.example")
    assert (status, out["supported"], out["status"]) == (0, False, "unknown")
    assert "guide" not in out
    status, _, err = mk("account", "detect", "nonsense")
    assert status == 1 and err.startswith("mk: ")


def test_cli_edit_needs_a_change(mk):
    mk("account", "add", "工作", "--address", WORK, "--provider", "feishu", stdin="pw\n")
    status, _, err = mk("account", "edit", "工作")
    assert status == 1 and "nothing to change" in err
    status, _, err = mk("account", "edit", "工作", "--pause", "--resume")
    assert status == 1

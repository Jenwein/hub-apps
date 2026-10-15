"""Drafts and sending: new mail, replies and forwards, `draft show` before `send`, the size
limit, the copy in Sent, and what a failed send says. Mail is received through a real poll on
FakeServers, so a reply works from the index exactly as it does for the user.

The first mailbox is `工作` (Feishu) at `me+x@corp.example`: a Chinese short name and a `+tag`
address; the second is `QQ` at `me@qq.com`. Both are the user's own addresses, which a reply
must never go to by mistake."""

import email
import email.policy
import email.utils
import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mailfakes import FakeServer, fake_connect, make_raw
from mailkit import accounts, cli, drafts, poll, server
from mailkit.config import MailError
from mailkit.message import attachment_bytes, parse
from mailkit.providers import PROVIDERS
from mailkit.server import LoginError, SendRefused, ServerError
from mailkit.store import Store

SEED_MAIL = Path(__file__).parents[1] / "mail" / "seed" / "mail"
NOW = datetime(2026, 10, 14, 4, 0, tzinfo=UTC)
WORK = "me+x@corp.example"
QQ = "me@qq.com"
PASSWORD = "s3cretpass"
SENT = ("\\Sent",)
PDF = b"%PDF-1.4 resume of Zhang San\n" * 50


@pytest.fixture
def hub_dir(tmp_path):
    hub = tmp_path / "hub"
    shutil.copytree(SEED_MAIL, hub / "mail")
    (hub / "secrets").mkdir()
    return hub


def add(hub, fake, name, address, provider):
    result = accounts.add(hub, hub / "mail", name, address, PASSWORD, provider=provider,
                          connect=fake_connect(fake), now=NOW)
    assert result["ok"], result


class World:
    """工作 (Feishu, folders Sent and Trash flagged with SPECIAL-USE) and, unless `qq` is
    False, QQ (a Sent folder named 已发送, flagged \\Sent)."""

    def __init__(self, hub, *, qq=True):
        self.hub = hub
        self.work = FakeServer(folders={"Sent": {}, "Trash": {}},
                               folder_flags={"Sent": SENT, "Trash": ("\\Trash",)})
        self.qq = FakeServer(folders={"已发送": {}}, folder_flags={"已发送": SENT})
        add(hub, self.work, "工作", WORK, "feishu")
        self.servers = {WORK: self.work}
        if qq:
            add(hub, self.qq, "QQ", QQ, "qq")
            self.servers[QQ] = self.qq
        self.minutes = 0

    @property
    def mail(self):
        return self.hub / "mail"

    def connect(self):
        return fake_connect(self.servers)

    def receive(self, subject, *, sender="boss@x.example", to=WORK, fake=None, **options) -> int:
        """A message arrives and is polled into the index; returns its id."""
        raw = make_raw(subject, sender, to, f"body of {subject}", **options)
        return self.receive_raw(raw, subject, fake=fake)

    def receive_raw(self, raw: bytes, subject: str, *, fake=None) -> int:
        """Raw bytes arrive and are polled into the index; returns the id of `subject`."""
        (fake or self.work).deliver(raw)
        self.minutes += 1
        poll.poll(self.hub, self.mail, connect=self.connect(),
                  now=NOW + timedelta(minutes=self.minutes))
        with Store.open(self.mail) as store:
            items = store.search("", per_page=1000)["items"]
        return max(item["id"] for item in items if item["subject"] == subject)

    def draft(self, body="好的，周三见。", *, mailbox=None, to=(), cc=(), subject=None,
              attach=(), **options) -> str:
        return self.create(body, mailbox=mailbox, to=to, cc=cc, subject=subject,
                           attach=attach, **options)["draft_id"]

    def create(self, body="好的，周三见。", *, mailbox=None, to=(), cc=(), subject=None,
               attach=(), **options) -> dict:
        return drafts.create(
            self.hub, self.mail, mailbox=mailbox, to=list(to), cc=list(cc), subject=subject,
            body=body, attach=list(attach), connect=self.connect(), **options)

    def stored(self, draft_id) -> dict:
        return drafts.read(self.mail, draft_id)

    def show(self, draft_id) -> str:
        return drafts.show(self.mail, draft_id)

    def send(self, draft_id) -> dict:
        return drafts.send(self.hub, self.mail, draft_id, connect=self.connect())

    def edit(self, draft_id, **changes) -> dict:
        return drafts.edit(self.mail, draft_id, hub=self.hub, **changes)

    def draft_files(self) -> list[Path]:
        folder = self.mail / ".state" / "drafts"
        return sorted(folder.glob("*.json")) if folder.exists() else []


@pytest.fixture
def world(hub_dir):
    return World(hub_dir)


def recipients(world, draft_id) -> tuple[list[str], list[str]]:
    stored = world.stored(draft_id)
    return stored["to"], stored["cc"]


def as_email(raw: bytes):
    return email.message_from_bytes(raw, policy=email.policy.default)


# -- replies ---------------------------------------------------------------------------------


def test_reply_defaults(world):
    mid = world.receive(
        "面试安排", sender="Boss <boss@x.example>", to=WORK, cc="c@x.example",
        headers={"Message-ID": "<orig@x.example>", "References": "<root@x.example>"})
    d = world.draft(reply_to=mid)
    stored = world.stored(d)
    assert (stored["mailbox"], stored["address"]) == ("工作", WORK)  # the receiving mailbox
    assert (stored["to"], stored["cc"]) == (["boss@x.example"], [])
    assert stored["subject"] == "Re: 面试安排"

    world.show(d)
    result = world.send(d)
    assert result["ok"] and result["mailbox"] == "工作"
    [sent] = world.work.sent
    assert sent.sender == WORK and sent.recipients == ["boss@x.example"]
    msg = as_email(sent.raw)
    assert msg["In-Reply-To"] == "<orig@x.example>"
    assert msg["References"].split() == ["<root@x.example>", "<orig@x.example>"]
    assert msg["Subject"] == "Re: 面试安排"
    assert world.qq.sent == []

    again = world.receive("Re: 面试安排", headers={"Message-ID": "<r2@x.example>"})
    assert world.stored(world.draft(reply_to=again))["subject"] == "Re: 面试安排"  # no "Re: Re:"

    everyone = world.draft(reply_to=mid, reply_all=True)
    assert recipients(world, everyone) == (["boss@x.example"], ["c@x.example"])


def test_reply_all_leaves_out_every_address_of_the_user_and_every_repeat(world):
    mid = world.receive(
        "plan", sender="boss@x.example",
        to=["ME@corp.example", "d@x.example", "Boss@x.example"],
        cc=["me+other@corp.example", "ME@QQ.COM", "c@x.example", "d@x.example", "C@x.example"])
    d = world.draft(reply_to=mid, reply_all=True, cc=["e@x.example", "c@x.example"])
    assert recipients(world, d) == (["boss@x.example", "d@x.example"],
                                    ["c@x.example", "e@x.example"])
    assert recipients(world, world.draft(reply_to=mid)) == (["boss@x.example"], [])


def test_a_reply_goes_to_the_reply_to_address_when_the_original_has_one(world):
    mid = world.receive("job", sender="noreply@hr.example", cc="c@x.example",
                        headers={"Reply-To": "=?utf-8?B?5Lq65LqL?= <hr@corp.example>"})
    created = world.create(reply_to=mid)
    assert recipients(world, created["draft_id"]) == (["hr@corp.example"], [])
    assert any("Reply-To" in w and "noreply@hr.example" in w for w in created["warnings"])
    same = world.receive("same", sender="boss@x.example", headers={"Reply-To": "Boss@x.example"})
    assert world.create(reply_to=same)["warnings"] == []
    assert recipients(world, world.draft(reply_to=mid, reply_all=True)) == (
        ["hr@corp.example"], ["c@x.example"])
    own = world.receive("list", sender="list@x.example", headers={"Reply-To": WORK})
    assert recipients(world, world.draft(reply_to=own)) == (["list@x.example"], [])


def test_reply_to_own_sent_message_goes_to_original_recipients(world):
    mid = world.receive("周报", sender=f"我 <{WORK}>", to=["boss@x.example", WORK],
                        cc=["c@x.example", QQ], headers={"Reply-To": WORK})
    d = world.draft(reply_to=mid)
    assert recipients(world, d) == (["boss@x.example"], [])
    assert recipients(world, world.draft(reply_to=mid, reply_all=True)) == (
        ["boss@x.example"], ["c@x.example"])
    world.show(d)
    world.send(d)
    assert world.work.sent[0].recipients == ["boss@x.example"]

    # another case and no +tag: still the user
    other = world.receive("周报二", sender="ME@corp.example", to="boss@x.example")
    assert recipients(world, world.draft(reply_to=other)) == (["boss@x.example"], [])
    # sent from the user's other mailbox
    qq = world.receive("周报三", sender=QQ, to=["boss@x.example", WORK])
    assert recipients(world, world.draft(reply_to=qq)) == (["boss@x.example"], [])
    # a note to self is answered to self
    note = world.receive("note", sender=WORK, to=WORK)
    assert recipients(world, world.draft(reply_to=note)) == ([WORK], [])


def test_a_reply_without_references_threads_on_in_reply_to(world):
    mid = world.receive("x", headers={"Message-ID": "<b@x.example>",
                                      "In-Reply-To": "<a@x.example>"})
    stored = world.stored(world.draft(reply_to=mid))
    assert stored["in_reply_to"] == "<b@x.example>"
    assert stored["references"] == ["<a@x.example>", "<b@x.example>"]


def test_a_long_thread_keeps_the_first_and_the_last_references(world):
    refs = " ".join(f"<r{n}@x.example>" for n in range(40))
    mid = world.receive("x", headers={"Message-ID": "<m@x.example>", "References": refs})
    kept = world.stored(world.draft(reply_to=mid))["references"]
    assert len(kept) == drafts.MAX_REFERENCES
    assert kept[0] == "<r0@x.example>" and kept[-2:] == ["<r39@x.example>", "<m@x.example>"]


def test_long_message_ids_are_folded_but_never_encoded(world):
    local = ("DM6PR11MB4691" + "0F1C2E3D4B5A6978" * 2)[:43]
    long_id = f"<{local}@DM6PR11MB4691.namprd11.prod.outlook.com>"  # an Exchange-style id
    assert len(long_id) == 85
    refs = [f"<{'r' * 50}{n}@x.example>" for n in range(3)]
    mid = world.receive("plan", headers={"Message-ID": long_id, "References": " ".join(refs)})
    d = world.draft(reply_to=mid)
    assert world.stored(d)["references"] == [*refs, long_id]
    world.show(d)
    world.send(d)
    raw = world.work.sent[0].raw
    head = raw.split(b"\r\n\r\n", 1)[0]
    assert f"In-Reply-To: {long_id}".encode() in head
    assert long_id.encode() in head.split(b"References:", 1)[1]
    assert b"=?" not in raw
    assert all(len(line) <= 998 for line in head.split(b"\r\n"))
    got = parse(raw)
    assert got.in_reply_to == long_id and got.references == [*refs, long_id]
    assert as_email(raw)["References"].split() == [*refs, long_id]


def test_ids_that_are_not_plain_ascii_are_left_out_of_the_thread_headers(world):
    def raw(subject, *headers):
        return "\r\n".join(["From: boss@x.example", f"To: {WORK}", f"Subject: {subject}",
                            *headers, "", "hi", ""]).encode("utf-8")  # 8-bit header bytes

    mid = world.receive_raw(raw("x", "Message-ID: <ok@x.example>",
                                "References: <ünï@x.example> <fine@x.example>"), "x")
    stored = world.stored(world.draft(reply_to=mid))
    assert (stored["in_reply_to"], stored["references"]) == (
        "<ok@x.example>", ["<fine@x.example>", "<ok@x.example>"])
    odd = world.receive_raw(raw("y", "Message-ID: <ïd@x.example>",
                                "In-Reply-To: <ünï@x.example>"), "y")
    stored = world.stored(world.draft(reply_to=odd))
    assert (stored["in_reply_to"], stored["references"]) == (None, [])
    d = world.draft(reply_to=odd)
    world.show(d)
    world.send(d)
    msg = as_email(world.work.sent[0].raw)
    assert msg["In-Reply-To"] is None and msg["References"] is None


def test_show_says_when_a_reply_does_not_go_to_the_sender(world):
    mid = world.receive("job", sender="noreply@hr.example", headers={"Reply-To": "hr@corp.example"})
    d = world.draft(reply_to=mid)
    lines = world.show(d).splitlines()
    note = (f"Note: To is the Reply-To of message {mid} (hr@corp.example), not its sender "
            "noreply@hr.example")
    assert lines[lines.index("Cc: (none)") + 1] == note

    own = world.receive("周报", sender=WORK, to="boss@x.example")
    assert (f"Note: message {own} is from your own address {WORK}, so the reply goes to its "
            "recipients") in world.show(world.draft(reply_to=own)).splitlines()
    self_note = world.receive("note", sender=WORK, to=WORK)
    assert (f"Note: message {self_note} is from your own address {WORK} and went only to you, "
            "so the reply goes back to you") in world.show(
                world.draft(reply_to=self_note)).splitlines()

    plain = world.receive("plain")
    assert not [line for line in world.show(world.draft(reply_to=plain)).splitlines()
                if line.startswith("Note:")]
    world.edit(d, to=["boss@x.example"])  # the user chose To: the note no longer applies
    assert not [line for line in world.show(d).splitlines() if line.startswith("Note:")]


@pytest.mark.parametrize("original, expected", [
    ("面试安排", "Re: 面试安排"),
    ("Re: 面试安排", "Re: 面试安排"),
    ("RE: x", "RE: x"),
    ("re:x", "re:x"),
    ("回复: x", "回复: x"),
    ("回复：x", "回复：x"),
    ("Re：x", "Re：x"),
    ("答复: x", "答复: x"),
    ("Re[2]: x", "Re[2]: x"),
    ("Fwd: x", "Re: Fwd: x"),
    ("Regards", "Re: Regards"),
    ("", "Re:"),
])
def test_reply_subject(original, expected):
    assert drafts.reply_subject(original) == expected


@pytest.mark.parametrize("original, expected", [
    ("简历", "Fwd: 简历"),
    ("Fwd: x", "Fwd: x"),
    ("FW: x", "FW: x"),
    ("转发: x", "转发: x"),
    ("Fwd：x", "Fwd：x"),
    ("Re: x", "Fwd: Re: x"),
    ("Fwdx", "Fwd: Fwdx"),
])
def test_forward_subject(original, expected):
    assert drafts.forward_subject(original) == expected


# -- forwards --------------------------------------------------------------------------------


def test_forward_carries_original_attachments(world, tmp_path):
    mid = world.receive("简历", sender="张三 <zhang@x.example>",
                        attachments=[("简历-张三.pdf", PDF), ("note.txt", b"hello")])
    cover = tmp_path / "cover.txt"
    cover.write_bytes(b"cover letter")
    d = world.draft("请看附件。", to=["hr@corp.example"], forward_of=mid, attach=[cover])
    stored = world.stored(d)
    assert (stored["mailbox"], stored["subject"]) == ("工作", "Fwd: 简历")
    assert [a["name"] for a in stored["attachments"]] == ["简历-张三.pdf", "note.txt", "cover.txt"]

    text = world.show(d)
    assert "简历-张三.pdf" in text and "Forwarded message" in text
    assert text.index("请看附件。") < text.index("From: 张三 <zhang@x.example>")
    assert text.index("From: 张三 <zhang@x.example>") < text.index("body of 简历")

    world.send(d)
    [sent] = world.work.sent
    assert sent.recipients == ["hr@corp.example"]
    got = parse(sent.raw)
    assert [a.filename for a in got.attachments] == ["简历-张三.pdf", "note.txt", "cover.txt"]
    assert attachment_bytes(sent.raw, 0) == ("简历-张三.pdf", PDF)
    assert attachment_bytes(sent.raw, 1)[1] == b"hello"
    assert attachment_bytes(sent.raw, 2)[1] == b"cover letter"
    assert got.text.startswith("请看附件。") and "body of 简历" in got.text
    assert as_email(sent.raw)["In-Reply-To"] is None
    assert world.work.flags == {}  # the original stays unread


def test_forward_of_a_message_whose_attachments_cannot_be_fetched_makes_no_draft(world):
    mid = world.receive("简历", attachments=[("a.pdf", PDF)])
    world.work.inbox.clear()  # deleted by another mail client
    with pytest.raises(MailError, match="attachments") as caught:
        world.draft(to=["hr@corp.example"], forward_of=mid)
    assert "no longer in INBOX" in str(caught.value)
    assert world.draft_files() == []
    plain = world.receive("plain")
    world.work.inbox.clear()
    assert world.stored(world.draft(to=["hr@corp.example"], forward_of=plain))["attachments"] == []


def test_a_forward_needs_someone_to_go_to(world):
    mid = world.receive("x")
    with pytest.raises(MailError, match="To"):
        world.draft(forward_of=mid)


# -- show before send ------------------------------------------------------------------------


def test_send_requires_show_and_unchanged(world):
    mid = world.receive("面试安排")
    d = world.draft(reply_to=mid)
    with pytest.raises(MailError, match="show"):
        world.send(d)
    world.show(d)
    world.edit(d, body="changed")
    with pytest.raises(MailError, match="changed"):
        world.send(d)
    assert "send" not in world.work.names()
    world.show(d)
    assert world.send(d)["ok"]
    assert world.work.sent[0].recipients == ["boss@x.example"]
    assert parse(world.work.sent[0].raw).text.strip() == "changed"
    assert world.draft_files() == []
    with pytest.raises(MailError, match="no draft"):
        world.send(d)


@pytest.mark.parametrize("change", [
    {"to": ["other@x.example"]}, {"cc": ["c@x.example"]}, {"subject": "other"},
    {"mailbox": "QQ"}, {"drop": [1]},
])
def test_any_change_after_show_needs_a_new_show(world, tmp_path, change):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(PDF)
    d = world.draft(mailbox="工作", to=["boss@x.example"], subject="s", attach=[pdf])
    world.show(d)
    world.edit(d, **change)
    with pytest.raises(MailError, match="changed"):
        world.send(d)


def test_an_attachment_changed_on_disk_after_show_is_refused(world, tmp_path):
    pdf = tmp_path / "plan.pdf"
    pdf.write_bytes(PDF)
    d = world.draft(mailbox="工作", to=["boss@x.example"], subject="plan", attach=[pdf])
    world.show(d)
    for content in (PDF + b"x", PDF.replace(b"r", b"R")):  # longer; same size
        pdf.write_bytes(content)
        with pytest.raises(MailError, match="changed"):
            world.send(d)
    pdf.unlink()
    with pytest.raises(MailError, match="changed"):
        world.send(d)
    assert world.work.sent == []
    pdf.write_bytes(PDF)  # exactly what was shown
    world.send(d)
    assert attachment_bytes(world.work.sent[0].raw, 0) == ("plan.pdf", PDF)


def test_an_attachment_changed_between_create_and_show_is_refused_at_show(world, tmp_path):
    pdf = tmp_path / "plan.pdf"
    pdf.write_bytes(b"short")
    d = world.draft(mailbox="工作", to=["boss@x.example"], subject="plan", attach=[pdf])
    pdf.write_bytes(PDF)  # the file is not what was attached any more
    with pytest.raises(MailError, match="attachment 1 .*changed on disk since it was attached"):
        world.show(d)
    with pytest.raises(MailError, match="never shown"):  # the failed show recorded nothing
        world.send(d)
    world.edit(d, drop=[1], attach=[pdf])  # attached again: its content now
    assert "plan.pdf (1.4 KB)" in world.show(d)
    world.send(d)
    assert attachment_bytes(world.work.sent[0].raw, 0)[1] == PDF


def test_show_prints_the_whole_draft(world, tmp_path):
    pdf = tmp_path / "简历.pdf"
    pdf.write_bytes(b"x" * 2048)
    d = world.draft("第一行\n第二行", mailbox="工作", to=["boss@x.example", "d@x.example"],
                    cc=["c@x.example"], subject="面试", attach=[pdf])
    assert world.show(d).splitlines() == [
        f"Draft {d} (a new mail), not sent yet",
        "From: 工作 <me+x@corp.example>",
        "To: boss@x.example, d@x.example",
        "Cc: c@x.example",
        "Subject: 面试",
        "Attachments: 1",
        "  1. 简历.pdf (2.0 KB)",
        "Size: 2.0 KB in all (Feishu Mail takes up to 36 MB)",
        "--- text: 2 lines ---",
        "第一行",
        "第二行",
        "--- end of text ---",
    ]
    mid = world.receive("面试安排")
    r = world.draft("", reply_to=mid)
    lines = world.show(r).splitlines()
    assert lines[0] == f"Draft {r} (a reply to message {mid}), not sent yet"
    assert "Cc: (none)" in lines and "Attachments: none" in lines
    assert lines[-2:] == ["--- text: 0 lines ---", "--- end of text ---"]


def test_size_limit(world, tmp_path):
    big = tmp_path / "video.mp4"
    with big.open("wb") as stream:
        stream.truncate(37 * 1024 * 1024)
    created = world.create(mailbox="工作", to=["boss@x.example"], subject="video", attach=[big])
    assert any("36 MB" in warning for warning in created["warnings"])
    d = created["draft_id"]
    warnings = [line for line in world.show(d).splitlines() if line.startswith("WARNING")]
    assert len(warnings) == 1 and "36 MB" in warnings[0] and "Feishu Mail" in warnings[0]
    with pytest.raises(MailError, match="too large"):
        world.send(d)
    assert "send" not in world.work.names()

    world.edit(d, mailbox="QQ")  # QQ takes 50 MB
    assert "WARNING" not in world.show(d)
    assert world.send(d)["ok"] and len(world.qq.sent) == 1


# -- the copy in Sent ------------------------------------------------------------------------


@pytest.mark.parametrize("saves_sent, copies", [(None, 1), (False, 1), (True, 0)])
def test_sent_copy_once(world, monkeypatch, saves_sent, copies):
    monkeypatch.setitem(PROVIDERS, "feishu", replace(PROVIDERS["feishu"], saves_sent=saves_sent))
    d = world.draft(mailbox="工作", to=["boss@x.example"], subject="hi")
    world.show(d)
    result = world.send(d)
    appended = [call for call in world.work.calls if call[0] == "append"]
    assert len(appended) == copies
    if copies:
        assert appended[0][1:] == ("Sent", world.work.sent[0].raw)
        assert list(world.work.folders_["Sent"].values()) == [world.work.sent[0].raw]
        assert result["sent_copy"] == "Sent"
    else:
        assert result["sent_copy"] == "server" and "folders" not in world.work.names()
    assert result["warnings"] == []


def test_sent_copy_goes_to_the_flagged_folder_else_the_provider_name(world, monkeypatch):
    d = world.draft(mailbox="QQ", to=["boss@x.example"], subject="hi")
    world.show(d)
    assert world.send(d)["sent_copy"] == "已发送"

    monkeypatch.setitem(PROVIDERS, "qq", replace(PROVIDERS["qq"], sent=("Sent Messages",)))
    world.qq.folders_, world.qq.folder_flags = {"Sent Messages": {}}, {}
    d = world.draft(mailbox="QQ", to=["boss@x.example"], subject="hi")
    world.show(d)
    assert world.send(d)["sent_copy"] == "Sent Messages"


def test_a_copy_that_cannot_be_saved_is_a_warning_and_the_mail_is_sent(world):
    world.work.raise_on["append"] = ServerError(f"NO [OVERQUOTA] {PASSWORD}")
    d = world.draft(mailbox="工作", to=["boss@x.example"], subject="hi")
    world.show(d)
    result = world.send(d)
    assert result["ok"] and result["sent_copy"] is None and len(world.work.sent) == 1
    assert any("OVERQUOTA" in w and "Sent" in w for w in result["warnings"])
    assert PASSWORD not in json.dumps(result, ensure_ascii=False)
    assert world.draft_files() == []

    world.work.raise_on.clear()
    del world.work.folders_["Sent"]
    d = world.draft(mailbox="工作", to=["boss@x.example"], subject="hi")
    world.show(d)
    result = world.send(d)
    assert result["ok"] and result["sent_copy"] is None
    assert any("no Sent folder" in w for w in result["warnings"])


# -- choosing the mailbox --------------------------------------------------------------------


def test_new_mail_without_mailbox_asks(world):
    with pytest.raises(MailError, match="which mailbox") as caught:
        world.draft(to=["boss@x.example"], subject="hi")
    assert "工作" in str(caught.value) and "QQ" in str(caught.value)
    assert world.draft_files() == []


def test_new_mail_with_one_mailbox_uses_it(hub_dir):
    world = World(hub_dir, qq=False)
    assert world.stored(world.draft(to=["boss@x.example"], subject="hi"))["mailbox"] == "工作"


def test_the_mailbox_can_be_named_and_a_reply_can_go_out_from_another(world):
    assert world.stored(world.draft(mailbox="QQ", to=["a@x.example"], subject="s"))[
        "address"] == QQ
    mid = world.receive("x")
    assert world.stored(world.draft(reply_to=mid, mailbox="QQ"))["address"] == QQ
    with pytest.raises(MailError, match="no mailbox"):
        world.draft(mailbox="nowhere", to=["a@x.example"], subject="s")


# -- what a failed send says -----------------------------------------------------------------


@pytest.mark.parametrize("error, maybe", [
    (LoginError("smtp-login", "the SMTP server refused the login: 535 nope"), False),
    (SendRefused("the SMTP server refused the message: 552 too big"), False),
    (ServerError("sending through smtp.fake.example:465 failed: Connection closed"), True),
    (TimeoutError("timed out"), True),
])
def test_a_failed_send_keeps_the_draft_and_says_whether_the_mail_may_have_gone(
        world, error, maybe):
    d = world.draft(mailbox="工作", to=["boss@x.example"], subject="hi")
    world.show(d)
    world.work.raise_on["send"] = error
    with pytest.raises(MailError) as caught:
        world.send(d)
    message = str(caught.value)
    if maybe:
        assert "may have been sent" in message and "Sent" in message
    else:
        assert "not sent" in message and "may have been sent" not in message
    assert d in message and world.stored(d)["subject"] == "hi"
    assert "append" not in world.work.names()
    world.work.raise_on.clear()
    assert world.send(d)["ok"]  # the user said send; the draft is unchanged


def test_recipients_the_server_refused_are_reported(world):
    world.work.refuse = {"bad@x.example": "550 5.1.1 no such user"}
    d = world.draft(mailbox="工作", to=["boss@x.example", "bad@x.example"], subject="hi")
    world.show(d)
    result = world.send(d)
    assert result["ok"] and result["refused"] == {"bad@x.example": "550 5.1.1 no such user"}
    assert any("bad@x.example" in w for w in result["warnings"])
    assert world.work.sent[0].recipients == ["boss@x.example"]

    world.work.refuse["boss@x.example"] = "550 no"
    d = world.draft(mailbox="工作", to=["boss@x.example", "bad@x.example"], subject="hi")
    world.show(d)
    with pytest.raises(MailError, match="not sent"):
        world.send(d)
    assert world.stored(d)


def test_a_sent_draft_that_cannot_be_deleted_is_marked_and_never_sent_again(world, monkeypatch):
    d = world.draft(mailbox="工作", to=["boss@x.example"], subject="hi")
    world.show(d)

    def refuse(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", refuse)
        result = world.send(d)
    assert result["ok"] and any("marked as sent" in w for w in result["warnings"])
    with pytest.raises(MailError, match="was sent at"):
        world.send(d)
    assert len(world.work.sent) == 1


def test_the_password_is_in_no_draft_and_no_error(world):
    d = world.draft(mailbox="工作", to=["boss@x.example"], subject="hi")
    world.show(d)
    assert PASSWORD not in world.draft_files()[0].read_text("utf-8")
    world.work.raise_on["send"] = ServerError(f"NO login {PASSWORD} failed")
    with pytest.raises(MailError) as caught:
        world.send(d)
    assert PASSWORD not in str(caught.value)


def test_the_hubs_secrets_and_env_files_are_never_attached(world, tmp_path):
    secret = accounts.secret_path(world.hub, WORK)
    assert secret.is_file()
    other_case = world.hub / "SECRETS" / "mail" / secret.name
    env = tmp_path / ".env.local"
    env.write_text("TOKEN=x", "utf-8")
    for path in (secret, other_case, env, world.hub / "secrets"):
        with pytest.raises(MailError, match="never sent") as caught:
            world.draft(mailbox="工作", to=["a@x.example"], subject="s", attach=[path])
        assert PASSWORD not in str(caught.value)
    assert world.draft_files() == []
    d = world.draft(mailbox="工作", to=["a@x.example"], subject="s")
    with pytest.raises(MailError, match="never sent"):
        world.edit(d, attach=[secret])
    assert world.stored(d)["attachments"] == []


def test_send_refuses_a_draft_that_holds_the_password(world, tmp_path):
    def refused(draft_id, where):
        world.show(draft_id)
        with pytest.raises(MailError, match=f"password of 工作 in {where}") as caught:
            world.send(draft_id)
        assert PASSWORD not in str(caught.value)

    refused(world.draft(f"my code is {PASSWORD}", mailbox="工作", to=["a@x.example"],
                        subject="s"), "the text")
    refused(world.draft(mailbox="工作", to=["a@x.example"], subject=f"code {PASSWORD}"),
            "the subject")
    notes = tmp_path / "notes.txt"
    notes.write_bytes(b"login: " + PASSWORD.encode())
    refused(world.draft(mailbox="工作", to=["a@x.example"], subject="s", attach=[notes]),
            "attachment 1")
    assert world.work.sent == [] and "send" not in world.work.names()


def test_a_mailbox_removed_after_show_cannot_send(world):
    d = world.draft(mailbox="QQ", to=["boss@x.example"], subject="hi")
    world.show(d)
    accounts.remove(world.hub, world.mail, "QQ")
    with pytest.raises(MailError, match="not in config.toml"):
        world.send(d)


# -- the message that goes out ---------------------------------------------------------------


def test_the_sent_message(world, tmp_path):
    pdf = tmp_path / "简历 张三.pdf"
    pdf.write_bytes(PDF)
    text = "你好，\n" + "长" * 100 + "\n.\nFrom here on"
    d = world.draft(text, mailbox="工作", to=["boss@x.example"], cc=["c@x.example"],
                    subject="面试 安排", attach=[pdf])
    world.show(d)
    result = world.send(d)
    [sent] = world.work.sent
    assert sent.recipients == ["boss@x.example", "c@x.example"]
    raw = sent.raw
    assert raw.isascii()  # 7-bit: headers and text encoded
    msg = as_email(raw)
    assert (msg["From"], msg["To"], msg["Cc"]) == (WORK, "boss@x.example", "c@x.example")
    assert msg["Subject"] == "面试 安排"
    assert msg["Message-ID"] == result["message_id"]
    assert result["message_id"].endswith("@corp.example>")
    assert email.utils.parsedate_to_datetime(msg["Date"]).tzinfo is not None
    assert msg["In-Reply-To"] is None and msg["References"] is None and msg["Bcc"] is None
    body = msg.get_body(("plain",)).get_content()
    assert body.replace("\r\n", "\n").rstrip("\n") == text  # (CRLF: the canonical form)
    [attached] = list(msg.iter_attachments())
    assert attached.get_filename() == "简历 张三.pdf" and attached.get_content() == PDF
    assert b"filename*=utf-8''" in raw  # RFC 2231
    assert result["to"] == ["boss@x.example"] and result["cc"] == ["c@x.example"]


# -- making and changing drafts --------------------------------------------------------------


@pytest.mark.parametrize("change, message", [
    ({"to": ["a,b@x.example"]}, "not an email address"),
    ({"to": ["Boss <boss@x.example>"]}, "not an email address"),
    ({"to": ["boss@x.example\r\nBcc: evil@x.example"]}, "not an email address"),
    ({"to": ["张三@例子.中国"]}, "not an email address"),
    ({"to": []}, "To"),
    ({"cc": ["c@x.example;d@x.example"]}, "not an email address"),
    ({"subject": "hi\r\nBcc: evil@x.example"}, "one line"),
    ({"subject": None}, "subject"),
    ({"reply_all": True}, "reply"),
])
def test_create_refuses(world, change, message):
    options = {"mailbox": "工作", "to": ["boss@x.example"], "subject": "hi", **change}
    with pytest.raises(MailError, match=message):
        world.draft(**options)
    assert world.draft_files() == []


def test_attachments_must_be_files_that_exist(world, tmp_path):
    for path, message in ((tmp_path / "missing.pdf", "does not exist"),
                          (tmp_path, "not a file")):
        with pytest.raises(MailError, match=message):
            world.draft(mailbox="工作", to=["a@x.example"], subject="s", attach=[path])
    assert world.draft_files() == []


def test_a_draft_is_a_reply_or_a_forward_of_a_known_message(world):
    mid = world.receive("x")
    with pytest.raises(MailError, match="not both"):
        world.draft(to=["a@x.example"], reply_to=mid, forward_of=mid)
    with pytest.raises(MailError, match="no message with id 999"):
        world.draft(reply_to=999)
    with pytest.raises(MailError, match="no message with id 999"):
        world.draft(to=["a@x.example"], forward_of=999)


def test_edit_changes_what_is_sent(world, tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    d = world.draft("v1", mailbox="工作", to=["boss@x.example"], cc=["c@x.example"],
                    subject="s1", attach=[a])
    out = world.edit(d, to=["d@x.example", "c@x.example"], cc=["c@x.example"], subject="s2",
                     body="v2", attach=[b], drop=[1], mailbox="QQ")
    assert out["draft_id"] == d and out["mailbox"] == "QQ"
    assert out["changed"] == ["attach", "body", "cc", "drop", "mailbox", "subject", "to"]
    stored = world.stored(d)
    assert (stored["address"], stored["to"], stored["cc"]) == (
        QQ, ["d@x.example", "c@x.example"], [])
    assert [x["name"] for x in stored["attachments"]] == ["b.pdf"]
    world.show(d)
    world.send(d)
    [sent] = world.qq.sent
    assert sent.recipients == ["d@x.example", "c@x.example"]
    assert parse(sent.raw).subject == "s2" and attachment_bytes(sent.raw, 0)[1] == b"b"


@pytest.mark.parametrize("changes, message", [
    ({}, "nothing to change"),
    ({"to": []}, "To"),
    ({"drop": [2]}, "no attachment 2"),
    ({"bcc": ["x@x.example"]}, "cannot change bcc"),
    ({"subject": "a\nb"}, "one line"),
])
def test_edit_refuses(world, changes, message):
    d = world.draft(mailbox="工作", to=["boss@x.example"], subject="s")
    with pytest.raises(MailError, match=message):
        world.edit(d, **changes)


@pytest.mark.parametrize("bad", ["../x", "ABCDEFGH", "abc", "abcdefg/", "", "abcdefgh.json"])
def test_draft_ids_that_are_not_ours_never_reach_the_file_system(world, bad):
    for call in (lambda: world.show(bad), lambda: world.send(bad),
                 lambda: world.edit(bad, subject="x")):
        with pytest.raises(MailError, match="not a draft id"):
            call()


def test_an_unknown_or_damaged_draft(world):
    with pytest.raises(MailError, match="no draft abcdefgh"):
        world.show("abcdefgh")
    d = world.draft(mailbox="工作", to=["boss@x.example"], subject="s")
    world.draft_files()[0].write_text("{not json", "utf-8")
    with pytest.raises(MailError, match="cannot be read"):
        world.show(d)


# -- the command line ------------------------------------------------------------------------


@pytest.fixture
def mk(world, monkeypatch, capsys):
    monkeypatch.setattr(server, "connect", world.connect())

    def run_mk(*args):
        status = cli.main(["--mail-dir", str(world.mail), "--hub", str(world.hub), *args])
        out, err = capsys.readouterr()
        return status, out, err

    return run_mk


def body_file(tmp_path, text: str, *, bom=False, name="body.txt") -> str:
    path = tmp_path / name
    path.write_bytes(("﻿" if bom else "").encode() + text.encode("utf-8"))
    return str(path)


def test_cli_new_show_send(world, mk, tmp_path):
    body = body_file(tmp_path, "你好，\r\n周三见。\r\n", bom=True)
    status, out, err = mk("draft", "new", "--from", "工作", "--to", "boss@x.example",
                          "--to", "d@x.example", "--cc", "c@x.example", "--subject", "面试",
                          "--body-file", body)
    assert (status, err) == (0, "")
    created = json.loads(out)
    d = created["draft_id"]
    assert created["mailbox"] == "工作"

    status, out, err = mk("send", d)
    assert status == 1 and out == "" and err.startswith("mk: ") and "show" in err

    status, out, err = mk("draft", "show", d)
    assert (status, err) == (0, "")
    assert out.startswith(f"Draft {d} ") and "To: boss@x.example, d@x.example" in out
    assert "你好，\n周三见。\n--- end of text ---" in out and "﻿" not in out

    status, out, err = mk("send", d)
    assert (status, err) == (0, "")
    result = json.loads(out)
    assert result["ok"] and result["draft_id"] == d
    [sent] = world.work.sent
    assert sent.recipients == ["boss@x.example", "d@x.example", "c@x.example"]
    assert parse(sent.raw).text.strip() == "你好，\n周三见。"


def test_cli_reply_forward_and_edit(world, mk, tmp_path):
    mid = world.receive("面试安排", cc="c@x.example", attachments=[("a.pdf", PDF)])
    body = body_file(tmp_path, "好的")
    status, out, err = mk("draft", "reply", str(mid), "--body-file", body, "--all")
    assert (status, err) == (0, "")
    d = json.loads(out)["draft_id"]
    assert recipients(world, d) == (["boss@x.example"], ["c@x.example"])

    status, out, _ = mk("draft", "edit", d, "--subject", "Re: 改期", "--no-cc")
    assert status == 0 and json.loads(out)["changed"] == ["cc", "subject"]
    assert world.stored(d)["subject"] == "Re: 改期" and world.stored(d)["cc"] == []

    status, out, err = mk("draft", "forward", str(mid), "--to", "hr@corp.example",
                          "--body-file", body)
    assert (status, err) == (0, "")
    f = json.loads(out)["draft_id"]
    assert [a["name"] for a in world.stored(f)["attachments"]] == ["a.pdf"]
    other = tmp_path / "其他.pdf"
    other.write_bytes(b"other")
    status, out, _ = mk("draft", "edit", f, "--drop", "1", "--attach", str(other))
    assert status == 0
    assert [a["name"] for a in world.stored(f)["attachments"]] == ["其他.pdf"]


@pytest.mark.parametrize("argv, message", [
    (["draft", "new", "--to", "a@x.example", "--subject", "s"], "--body-file"),
    (["draft", "new", "--to", "a@x.example", "--subject", "s", "--body-file", "nope.txt"],
     "body file"),
    (["draft", "reply", "x", "--body-file", "b.txt"], "not a number"),
    (["draft", "forward", "1", "--body-file", "b.txt"], "--to"),
    (["draft", "show"], "required"),
    (["draft", "edit", "abcdefgh"], "nothing to change"),
    (["draft", "edit", "abcdefgh", "--cc", "c@x.example", "--no-cc"], "not allowed with"),
    (["send"], "required"),
    (["send", "../x"], "not a draft id"),
])
def test_cli_usage_errors_are_mk_errors(mk, argv, message):
    status, out, err = mk(*argv)
    assert status == 1 and out == ""
    assert "mk: " in err and message in err and "Traceback" not in err


def test_cli_refuses_a_body_file_from_the_secrets(world, mk, tmp_path):
    secret = accounts.secret_path(world.hub, WORK)
    env = tmp_path / ".env"
    env.write_text("A=1", "utf-8")
    d = world.draft(mailbox="工作", to=["a@x.example"], subject="s")
    for path in (secret, env):
        for argv in (["draft", "new", "--from", "工作", "--to", "a@x.example", "--subject", "s",
                      "--body-file", str(path)],
                     ["draft", "edit", d, "--body-file", str(path)]):
            status, out, err = mk(*argv)
            assert status == 1 and out == "" and "never sent" in err and PASSWORD not in err
    assert world.stored(d)["body"] == "好的，周三见。"


def test_cli_a_body_file_that_is_not_utf8(mk, tmp_path):
    path = tmp_path / "gbk.txt"
    path.write_bytes("你好".encode("gbk"))
    status, out, err = mk("draft", "new", "--from", "工作", "--to", "a@x.example",
                          "--subject", "s", "--body-file", str(path))
    assert status == 1 and out == "" and "UTF-8" in err


def test_cli_drafts_and_send_are_not_in_the_bg_group():
    parser = cli.build_parser()
    top = next(a for a in parser._actions if a.dest == "command")
    bg = next(a for a in top.choices["bg"]._actions if a.dest == "action")
    for name in ("draft", "send"):
        assert name in top.choices and name not in bg.choices
    draft = next(a for a in top.choices["draft"]._actions if a.dest == "action")
    assert set(draft.choices) == {"new", "reply", "forward", "edit", "show"}

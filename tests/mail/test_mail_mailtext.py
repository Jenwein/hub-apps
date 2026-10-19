"""Mail as text (mailkit.mailtext): headers, body and attachments of odd mail.

The .eml fixtures are in fixtures/eml: plain UTF-8, HTML only in GB18030, a declared charset that
is wrong, a receipt.pdf next to an .exe, and a body of 20000 characters.
"""

import os
import subprocess
import time
from pathlib import Path

# Imported before any test patches subprocess: importing it may ask the platform module, which
# runs a command on Windows. Reading a PDF itself runs nothing.
import pdfplumber
import pytest
from mailkit import mailtext
from mailkit.mailtext import Attachment, attachment_bytes, attachment_texts, html_to_text, parse
from mailkit.reader import LOOK_BODY_LIMIT, TRIAGE_BODY_LIMIT, Reader
from mailkit.settings import MailError

FIXTURES = Path(__file__).parent / "fixtures"
EML = FIXTURES / "eml"


def eml(name: str) -> bytes:
    return (EML / name).read_bytes()


def test_plain_mail_headers_and_body():
    found = parse(eml("plain-utf8.eml"))
    assert found.sender_name == "张三"
    assert found.sender_addr == "zhang@example.cn"
    assert found.to == ["me@example.org", "bob@example.org"]
    assert found.cc == ["carol@example.org"]
    assert found.reply_to == ["replies@example.cn"]
    assert found.date == "2026-10-19T10:00:00+08:00"
    assert found.subject == "会议改到周五"
    assert found.message_id == "<plain-1@example.cn>"
    assert found.references == "<older-1@example.org> <older-2@example.org>"
    assert "会议改到周五下午三点" in found.body
    assert found.attachments == []


def test_gb18030_html_only_mail_reads_as_text():
    found = parse(eml("html-gb18030.eml"))
    assert found.subject == "您的订单已发货"
    assert "您好，您的订单已发货。" in found.body
    assert "运单号：SF1234567890" in found.body
    assert "价格 & 运费：¥88.00" in found.body  # entities unescaped
    assert "<" not in found.body and ">" not in found.body
    assert "alert" not in found.body  # the script is dropped
    assert "color" not in found.body  # the style is dropped
    assert found.attachments == []


def test_wrong_charset_still_readable():
    found = parse(eml("wrong-charset.eml"))  # says utf-8, holds GBK
    assert "本月账单金额：1,234.50 元，请于 2026-11-05 前还款。" in found.body
    assert "\ufffd" not in found.body


def test_an_unknown_charset_is_read_as_utf8_then_gb18030():
    for data in ("账单 ok".encode(), "账单 ok".encode("gb18030")):
        raw = (
            b"From: a@example.org\nSubject: x\n"
            b"Content-Type: text/plain; charset=x-no-such-charset\n\n" + data + b"\n")
        assert parse(raw).body.strip() == "账单 ok"


def test_raw_8bit_headers_are_read_as_utf8_or_gb18030():
    for charset in ("utf-8", "gb18030"):
        raw = (
            "From: 李四 <li@example.cn>\nSubject: 季度发票\nTo: me@example.org\n\nhi\n"
        ).encode(charset)
        found = parse(raw)
        assert found.subject == "季度发票", charset
        assert found.sender_name == "李四", charset
        assert found.sender_addr == "li@example.cn"


def test_header_values_are_one_line_without_control_characters():
    raw = (
        b"From: =?utf-8?q?Eve=0AInjected=E2=80=AE?= <eve@example.org>\n"
        b"Subject: =?utf-8?q?Hello=0D=0ABcc:_x@example.org?=\n"
        b"Message-ID: <a@b>\n\nbody\n")
    found = parse(raw)
    assert found.subject == "Hello Bcc: x@example.org"
    assert found.sender_name == "Eve Injected"
    assert "\n" not in found.subject + found.sender_name + found.message_id


def test_a_mail_without_a_text_part_has_an_empty_body_and_its_file_listed():
    raw = (
        b"From: a@example.org\nSubject: scan\nMIME-Version: 1.0\n"
        b'Content-Type: application/pdf; name="scan.pdf"\n'
        b"Content-Transfer-Encoding: base64\n\n"
        + __import__("base64").b64encode((FIXTURES / "receipt.pdf").read_bytes()) + b"\n")
    found = parse(raw)
    assert found.body == ""
    assert [a.filename for a in found.attachments] == ["scan.pdf"]
    assert attachment_texts(raw, 4000)[0][1] == "Total 88.00 CNY"


def test_an_empty_plain_part_gives_way_to_the_html_one():
    raw = (
        b"From: a@example.org\nSubject: x\nMIME-Version: 1.0\n"
        b'Content-Type: multipart/alternative; boundary="b"\n\n'
        b"--b\nContent-Type: text/plain; charset=utf-8\n\n \n"
        b"--b\nContent-Type: text/html; charset=utf-8\n\n<p>the real text</p>\n--b--\n")
    assert parse(raw).body == "the real text"


def test_pdf_attachment_text_and_exe_never_read(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("nothing may be run while reading attachments")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "system", refuse)
    if hasattr(os, "startfile"):
        monkeypatch.setattr(os, "startfile", refuse)
    raw = eml("receipt-and-exe.eml")
    pdf = (FIXTURES / "receipt.pdf").read_bytes()
    found = parse(raw)
    assert found.body.strip() == "Thank you. Your receipt is attached."
    assert found.attachments == [
        Attachment(1, "receipt.pdf", "application/pdf", len(pdf)),
        Attachment(2, "setup.exe", "application/x-msdownload", 282)]
    texts = attachment_texts(raw, 4000)
    assert [(a.filename, text) for a, text in texts] == [
        ("receipt.pdf", "Total 88.00 CNY"), ("setup.exe", "")]
    assert attachment_bytes(raw, 1) == (found.attachments[0], pdf)
    exe = attachment_bytes(raw, 2)[1]
    assert exe.startswith(b"MZ\x90\x00Total 99.00 CNY") and len(exe) == 282


def test_attachment_texts_of_text_parts_are_cut_to_the_limit():
    raw = (
        b"From: a@example.org\nSubject: x\nMIME-Version: 1.0\n"
        b'Content-Type: multipart/mixed; boundary="b"\n\n'
        b"--b\nContent-Type: text/plain; charset=utf-8\n\nbody\n"
        b"--b\nContent-Type: text/plain; charset=gbk\n"
        b'Content-Disposition: attachment; filename="notes.txt"\n\n'
        + ("备注" * 50).encode("gbk") + b"\n--b--\n")
    found = parse(raw)
    assert found.body.strip() == "body"
    [(attachment, text)] = attachment_texts(raw, 10)
    assert attachment.filename == "notes.txt"
    assert text == "备注" * 5


def test_attachment_names_are_cleaned_and_a_missing_name_gets_one():
    raw = (
        b"From: a@example.org\nSubject: x\nMIME-Version: 1.0\n"
        b'Content-Type: multipart/mixed; boundary="b"\n\n'
        b"--b\nContent-Type: text/plain\n\nbody\n"
        b"--b\nContent-Type: image/png\nContent-Disposition: attachment; filename=\""
        + b"=?utf-8?q?evil=E2=80=AEgpj.exe?=" + b"\"\n\nxx\n"
        b"--b\nContent-Type: application/octet-stream\n\nyy\n"
        b"--b\nContent-Type: application/zip\nContent-Disposition: attachment; filename=\""
        + b"a" * 300 + b".zip\"\n\nzz\n--b--\n")
    names = [a.filename for a in parse(raw).attachments]
    assert names[0] == "evilgpj.exe"
    assert names[1] == "attachment-2"
    assert len(names[2]) == 100 and names[2].endswith("…")


def test_an_unknown_attachment_number_is_an_error():
    with pytest.raises(MailError, match="no attachment 3"):
        attachment_bytes(eml("receipt-and-exe.eml"), 3)


def test_long_body_cut_at_limit(fake_claude):
    parsed = parse(eml("long-body.eml"))
    assert len(parsed.body) == 20000  # parse keeps all of it; the reader cuts
    reader = Reader("sonnet")
    reader.triage([("m/inbox/1", "m", parsed)], "", "en")
    reader.look("m/inbox/1", "m", parsed, [], "", "en")
    triage, look = (call["stdin"] for call in fake_claude.calls())
    assert parsed.body[:TRIAGE_BODY_LIMIT] in triage
    assert parsed.body[:TRIAGE_BODY_LIMIT + 1] not in triage
    assert parsed.body[:LOOK_BODY_LIMIT] in look
    assert parsed.body[:LOOK_BODY_LIMIT + 1] not in look
    assert len(triage) < TRIAGE_BODY_LIMIT + 1000
    assert len(look) < LOOK_BODY_LIMIT + 1000


def test_links_are_text_only():
    text = html_to_text(
        '<p>Pay now: <a href="https://evil.example/pay?u=1">your bank</a></p>'
        '<img src="https://evil.example/pixel.png" alt="x">')
    assert text == "Pay now: your bank"
    found = parse(eml("html-gb18030.eml"))
    assert "这里" in found.body
    assert "track.example.cn" not in found.body and "http" not in found.body


def test_html_blocks_become_lines_and_whitespace_collapses():
    text = html_to_text(
        "<h1>Title</h1><div>one\n   two</div><p>three<br>four<br/>five</p>"
        "<ul><li>a</li><li>b</li></ul><table><tr><td>x</td><td>y</td></tr></table>"
        "<script>var s = '<p>no</p>';</script><style>.c{}</style>tail &lt;b&gt;")
    assert text == "Title\none two\nthree\nfour\nfive\na\nb\nx y\ntail <b>"


def test_broken_html_still_gives_text():
    assert html_to_text("<p>open <b>never closed <div>x") == "open never closed\nx"
    assert html_to_text("<script>unclosed script") == ""
    assert "text" in html_to_text("<!-- a comment <p> --><p>text</p><![CDATA[x]]>")


# --- hostile structure -------------------------------------------------------------------------

DEEP = 2000  # far past what the stdlib parser and writer can recurse through


def nested_multipart(depth: int) -> bytes:
    opens = [
        f'Content-Type: multipart/mixed; boundary="b{n}"\n\n--b{n}\n' for n in range(depth)]
    closes = [f"\n--b{n}--\n" for n in reversed(range(depth))]
    return (
        "From: Deep <deep@example.org>\nSubject: deep multipart\nMIME-Version: 1.0\n"
        + "".join(opens) + "Content-Type: text/plain\n\ninner text\n" + "".join(closes)
    ).encode("ascii")


def nested_messages(depth: int) -> bytes:
    return (
        "From: Deep <deep@example.org>\nSubject: deep messages\nMIME-Version: 1.0\n"
        + "Content-Type: message/rfc822\n\n" * depth + "Subject: inner\n\ninner text\n"
    ).encode("ascii")


# 2000 levels break the parser; 200 (Python 3.12) and 600 (3.14) levels parse but break the
# writer that gives an attached message's bytes back.
@pytest.mark.parametrize("raw", [
    nested_multipart(DEEP), nested_messages(DEEP), nested_messages(200), nested_messages(600)],
    ids=["multipart-2000", "rfc822-2000", "rfc822-200", "rfc822-600"])
def test_deep_nesting_never_crashes(raw):
    found = parse(raw)
    assert (found.sender_name, found.sender_addr) == ("Deep", "deep@example.org")
    assert found.subject.startswith("deep ")
    assert found.body == ""
    texts = attachment_texts(raw, 4000)
    assert all(text == "" for _, text in texts)
    if found.attachments:
        attachment_bytes(raw, 1)
    else:
        with pytest.raises(MailError, match="no attachment 1"):
            attachment_bytes(raw, 1)


def test_a_deep_mail_keeps_its_headers_without_its_parts():
    found = parse(nested_multipart(DEEP))
    assert found.subject == "deep multipart"
    assert found.attachments == []


def test_a_pdf_over_the_size_cap_is_not_read(monkeypatch):
    assert mailtext.PDF_MAX_BYTES == 5 * 1024 * 1024
    raw = eml("receipt-and-exe.eml")
    size = len((FIXTURES / "receipt.pdf").read_bytes())

    def refuse(*args, **kwargs):
        raise AssertionError("a PDF over the cap must not be opened")

    monkeypatch.setattr(mailtext, "PDF_MAX_BYTES", size - 1)
    monkeypatch.setattr(pdfplumber, "open", refuse)
    assert attachment_texts(raw, 4000)[0][1] == ""
    monkeypatch.undo()
    monkeypatch.setattr(mailtext, "PDF_MAX_BYTES", size)
    assert attachment_texts(raw, 4000)[0][1] == "Total 88.00 CNY"


def test_pdfplumber_is_asked_for_the_first_ten_pages_only(monkeypatch):
    asked = {}
    real_open = pdfplumber.open

    def spy(stream, *args, **kwargs):
        asked.update(kwargs)
        return real_open(stream, *args, **kwargs)

    monkeypatch.setattr(pdfplumber, "open", spy)
    assert attachment_texts(eml("receipt-and-exe.eml"), 4000)[0][1] == "Total 88.00 CNY"
    assert list(asked["pages"]) == list(range(1, 11))


# --- long address lists ------------------------------------------------------------------------


def bulk_mail(names: list[str]) -> bytes:
    folded = ",\n ".join(", ".join(names[at:at + 3]) for at in range(0, len(names), 3))
    return (
        f"From: Mallory <mallory@example.org>\nTo: {folded}\nCc: {folded}\n"
        "Reply-To: a@example.org, b@example.org, c@example.org\n"
        "Subject: bulk\nContent-Type: text/plain\n\nthe body\n").encode()


def test_a_mail_with_300000_recipients_parses_quickly():
    raw = bulk_mail([f"user{n:06d}@example.org" for n in range(300_000)])
    start = time.monotonic()
    found = parse(raw)
    assert time.monotonic() - start < 5
    assert len(found.to) == len(found.cc) == mailtext.ADDRESS_LIMIT == 200
    assert found.to[:2] == ["user000000@example.org", "user000001@example.org"]
    assert found.to[-1] == "user000199@example.org"
    assert (found.sender_name, found.sender_addr) == ("Mallory", "mallory@example.org")
    assert (found.subject, found.body) == ("bulk", "the body\n")


def test_a_few_recipients_are_all_read():
    found = parse(bulk_mail(["x@example.org", "Y <y@example.org>", '"Doe, Jo" <jo@example.org>']))
    assert found.to == found.cc == ["x@example.org", "y@example.org", "jo@example.org"]
    assert found.reply_to == ["a@example.org", "b@example.org", "c@example.org"]


def test_a_long_list_is_cut_between_addresses():
    # 200 addresses of about 170 characters: the cut at 20000 characters comes first.
    names = [f'"{"N" * 140} {n:06d}" <user{n:06d}@example.org>' for n in range(200)]
    found = parse(bulk_mail(names))
    assert 100 < len(found.to) < 200
    assert found.to == [f"user{n:06d}@example.org" for n in range(len(found.to))]

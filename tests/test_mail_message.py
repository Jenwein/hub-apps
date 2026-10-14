import base64
import re
import time
from pathlib import Path

import mailkit.message as mm
import pytest
from mailkit.config import MailError
from mailkit.message import Attachment, addressed, attachment_bytes, parse

FIXTURES = Path(__file__).parent / "fixtures" / "mail"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def message(*lines: bytes, body: bytes = b"") -> bytes:
    """A raw message: the header lines, a blank line, the body."""
    return b"\r\n".join(lines) + b"\r\n\r\n" + body


def encoded_word(text: str, charset: str) -> bytes:
    return f"=?{charset}?B?".encode() + base64.b64encode(text.encode(charset)) + b"?="


def with_attachment(*headers: bytes) -> bytes:
    """A message whose second part is a PDF with the given part headers."""
    return message(
        b"From: a@x", b'Content-Type: multipart/mixed; boundary="B"',
        body=b"--B\r\nContent-Type: text/plain\r\n\r\nhello\r\n--B\r\n"
        + b"\r\n".join(headers) + b"\r\nContent-Transfer-Encoding: base64\r\n\r\n"
        + base64.b64encode(b"%PDF-1.4") + b"\r\n--B--\r\n")


# -- bodies and encodings --------------------------------------------------------------------


def test_gbk_body_and_subject():
    p = parse(fixture("gbk-plain.eml"))
    assert p.subject == "面试邀请" and "周三下午两点" in p.text
    assert p.from_name == "人事部" and p.from_addr == "hr@corp.example"


def test_gbk_without_charset_falls_back_to_gb18030():
    assert "工资" in parse(fixture("gbk-no-charset.eml")).text


def test_wrong_declared_charset_falls_back():
    body = "本月工资已发放".encode("gbk")
    for declared in (b"utf-8", b"us-ascii", b"gb2312", b"x-no-such-charset"):
        raw = message(b"From: a@x", b"Content-Type: text/plain; charset=" + declared, body=body)
        assert "工资已发放" in parse(raw).text, declared


def test_gb2312_label_with_gbk_only_characters():
    # 镕 is in GBK/GB18030 but not in GB2312, and mailers label such mail gb2312 anyway
    raw = message(b"From: a@x", b"Content-Type: text/plain; charset=gb2312",
                  body="朱镕基".encode("gb18030"))
    assert parse(raw).text == "朱镕基"


def test_utf8_without_charset_stays_readable():
    raw = message(b"From: a@x", b"Content-Type: text/plain", body="本月工资已发放".encode())
    assert parse(raw).text == "本月工资已发放"


def test_undecodable_bytes_are_replaced_not_raised():
    raw = message(b"From: a@x", b"Content-Type: text/plain; charset=utf-8",
                  body=b"ok \xff\xff\xfe \x81 end")
    assert parse(raw).text.startswith("ok ") and parse(raw).text.endswith(" end")


def test_raw_8bit_gbk_headers():
    raw = message(b"Subject: " + "面试邀请".encode("gbk"),
                  b"From: " + "张三".encode("gbk") + b" <zs@x.cn>",
                  b"To: a@x", b"Content-Type: text/plain", body=b"hi")
    p = parse(raw)
    assert p.subject == "面试邀请" and p.from_name == "张三" and p.from_addr == "zs@x.cn"


def test_encoded_word_with_unknown_charset_does_not_raise():
    raw = message(b"Subject: " + encoded_word("面试", "gbk").replace(b"gbk", b"nosuch"),
                  b"From: a@x", body=b"hi")
    assert parse(raw).text == "hi"


def test_html_only_becomes_text():
    t = parse(fixture("html-only.eml")).text
    assert "账单金额" in t and "<td>" not in t


def test_html_text_keeps_structure_and_drops_hidden_parts():
    t = parse(fixture("html-only.eml")).text
    assert t == "本月账单\n\n账单金额 ¥1,280.00\n到期日 2026-10-25\n\n请按时还款。\n谢谢 & 祝好"
    for hidden in ("color: red", "var cell", "<", ">", "&amp;", "\xa0"):
        assert hidden not in t
    assert t.count("账单") == 2  # the heading and the label, not the <title>


def html_text(html: str) -> str:
    raw = message(b"From: a@x", b"Content-Type: text/html; charset=utf-8", body=html.encode())
    return parse(raw).text


def test_html_line_breaks():
    assert html_text("a<br/>b<br>c<br><br>d") == "a\nb\nc\n\nd"
    assert html_text("<div>a</div><div><div>b</div></div>c") == "a\nb\nc"
    assert html_text("<ul><li>one</li><li>two</li></ul>") == "one\ntwo"
    assert html_text("<b>bold</b> <i>and</i> plain<script/> after") == "bold and plain after"
    assert html_text("<pre>x = 1\n  y = 2</pre>after") == "x = 1\ny = 2\n\nafter"


def test_html_unclosed_tags_and_entities_do_not_raise():
    raw = message(b"From: a@x", b"Content-Type: text/html; charset=utf-8",
                  body=b"<p>one<p>two &#38; &bogus; <b>bold</b><p>three <td")
    assert parse(raw).text.splitlines()[:3] == ["one", "", "two & &bogus; bold"]


def test_plain_text_is_preferred_over_html():
    p = parse(fixture("multipart-alternative.eml"))
    assert p.text == "本周计划：周五前提交报告。"
    assert "HTML copy" not in p.text


def test_only_plain_and_html_parts_are_the_body():
    raw = message(
        b"From: a@x", b'Content-Type: multipart/alternative; boundary="B"',
        body=b"--B\r\nContent-Type: text/plain\r\n\r\nthe invitation\r\n--B\r\n"
        b"Content-Type: text/calendar; method=REQUEST\r\n\r\nBEGIN:VCALENDAR\r\n--B--\r\n")
    assert parse(raw).text == "the invitation"


def test_empty_plain_part_falls_back_to_html():
    raw = message(
        b"From: a@x", b'Content-Type: multipart/alternative; boundary="B"',
        body=b"--B\r\nContent-Type: text/plain\r\n\r\n \r\n--B\r\n"
        b"Content-Type: text/html\r\n\r\n<p>from html</p>\r\n--B--\r\n")
    assert parse(raw).text == "from html"


def test_text_is_normalized_and_capped():
    raw = message(b"From: a@x", b"Content-Type: text/plain; charset=utf-8",
                  body=b"a  \r\n\r\n\r\n\r\nb\x00\xe2\x80\x8b c\r\n")
    assert parse(raw).text == "a\n\nb c"
    big = message(b"From: a@x", b"Content-Type: text/plain; charset=utf-8",
                  body=("工" * 150_000).encode())
    assert parse(big).text == "工" * 100_000


def test_attached_text_file_is_not_the_body():
    raw = message(
        b"From: a@x", b'Content-Type: multipart/mixed; boundary="B"',
        body=b"--B\r\nContent-Type: text/plain\r\n\r\nthe body\r\n--B\r\n"
        b"Content-Type: text/plain\r\nContent-Disposition: attachment; filename=n.txt\r\n\r\n"
        b"notes\r\n--B--\r\n")
    p = parse(raw)
    assert p.text == "the body" and [a.filename for a in p.attachments] == ["n.txt"]


# -- attachments -----------------------------------------------------------------------------


def test_rfc2231_chinese_filename():
    p = parse(fixture("rfc2231-attachment.eml"))
    assert [a.filename for a in p.attachments] == ["简历-张三.pdf"]
    name, data = attachment_bytes(fixture("rfc2231-attachment.eml"), 0)
    assert name == "简历-张三.pdf" and data.startswith(b"%PDF")


def test_attachment_fields():
    raw = fixture("rfc2231-attachment.eml")
    p = parse(raw)
    assert p.text == "您好，简历见附件。"
    [att] = p.attachments
    assert att == Attachment(index=0, filename="简历-张三.pdf", content_type="application/pdf",
                             size=len(attachment_bytes(raw, 0)[1]))
    assert att.size > 0


def test_attachment_filename_variants():
    pdf = b"Content-Type: application/pdf"
    gbk = "简历.pdf".encode("gbk")
    cases = {
        "encoded word": [pdf, b'Content-Disposition: attachment; filename="'
                         + encoded_word("简历.pdf", "gbk") + b'"'],
        "raw gbk": [pdf, b'Content-Disposition: attachment; filename="' + gbk + b'"'],
        "rfc2231 gbk": [pdf, b"Content-Disposition: attachment; filename*=gbk''%BC%F2%C0%FA.pdf"],
        "content-type name": [pdf + b'; name="' + gbk + b'"'],
        "encoded word name": [pdf + b'; name="' + encoded_word("简历.pdf", "gbk") + b'"'],
    }
    for label, headers in cases.items():
        raw = with_attachment(*headers)
        assert [a.filename for a in parse(raw).attachments] == ["简历.pdf"], label
        assert attachment_bytes(raw, 0) == ("简历.pdf", b"%PDF-1.4"), label


def test_attachment_filename_is_a_plain_name():
    raw = with_attachment(
        b"Content-Type: application/pdf",
        b'Content-Disposition: attachment; filename="..\\..\\evil/x.pdf"')
    [att] = parse(raw).attachments
    assert att.filename == "x.pdf"


def test_attachment_filename_cannot_disguise_itself():
    raw = with_attachment(
        b"Content-Type: application/pdf",
        b'Content-Disposition: attachment; filename="invoice\xe2\x80\xaefdp.exe\r\n\t.txt"')
    [att] = parse(raw).attachments
    assert att.filename == "invoicefdp.exe.txt"


def test_attachments_are_indexed_in_order():
    raw = message(
        b"From: a@x", b'Content-Type: multipart/mixed; boundary="B"',
        body=b"--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        b"--B\r\nContent-Type: image/png; name=one.png\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n" + base64.b64encode(b"11") + b"\r\n"
        b"--B\r\nContent-Type: application/zip\r\nContent-Disposition: attachment; "
        b"filename=two.zip\r\nContent-Transfer-Encoding: base64\r\n\r\n"
        + base64.b64encode(b"2222") + b"\r\n--B--\r\n")
    p = parse(raw)
    assert [(a.index, a.filename, a.size) for a in p.attachments] == [
        (0, "one.png", 2), (1, "two.zip", 4)]
    assert attachment_bytes(raw, 1) == ("two.zip", b"2222")


def test_attachment_index_out_of_range():
    raw = fixture("rfc2231-attachment.eml")
    for index in (1, -1, 7):
        with pytest.raises(MailError, match="attachment"):
            attachment_bytes(raw, index)
    with pytest.raises(MailError, match="attachment"):
        attachment_bytes(fixture("gbk-plain.eml"), 0)


# -- headers ---------------------------------------------------------------------------------


def test_bulk_and_threading_headers():
    p = parse(fixture("list-mail.eml"))
    assert p.bulk is True and p.in_reply_to == "<a@x>" and p.references == ["<root@x>", "<a@x>"]
    assert p.message_id == "<b@x>"


def test_bulk_detection():
    def bulk(*headers: bytes) -> bool:
        return parse(message(b"From: a@x", *headers, body=b"hi")).bulk

    assert not bulk()
    assert not bulk(b"Precedence: first-class")
    assert bulk(b"List-Unsubscribe: <mailto:u@x>")
    assert bulk(b"List-Id: <l.x>")
    assert bulk(b"Precedence: bulk")
    assert bulk(b"Precedence: List")
    assert not parse(fixture("gbk-plain.eml")).bulk


def test_no_threading_headers():
    p = parse(fixture("gbk-plain.eml"))
    assert p.in_reply_to is None and p.references == []
    assert p.message_id == "<gbk-1@corp.example>"
    p = parse(message(b"From: a@x", body=b"hi"))
    assert p.message_id is None and p.date is None


def test_threading_ids_are_taken_from_the_brackets():
    p = parse(message(b"From: a@x", b"In-Reply-To: <one@x> (the first) <two@x>",
                      b"References: <r1@x>  <r2@x>\t<r3@x>", b"Message-ID:  <m@x> ", body=b"hi"))
    assert p.in_reply_to == "<one@x>" and p.references == ["<r1@x>", "<r2@x>", "<r3@x>"]
    assert p.message_id == "<m@x>"


def test_date_is_iso_with_offset():
    assert parse(fixture("gbk-plain.eml")).date == "2026-10-14T09:30:00+08:00"
    assert parse(fixture("list-mail.eml")).date == "2026-10-14T14:00:00+00:00"
    unknown_zone = message(b"From: a@x", b"Date: Wed, 14 Oct 2026 09:30:00 -0000", body=b"hi")
    assert parse(unknown_zone).date == "2026-10-14T09:30:00+00:00"


def test_bad_date_is_none():
    for value in (b"yesterday", b"", b"Wed, 99 Foo 2026 99:99:99 +0800"):
        assert parse(message(b"From: a@x", b"Date: " + value, body=b"hi")).date is None


def test_addresses():
    p = parse(fixture("multipart-alternative.eml"))
    assert (p.from_name, p.from_addr) == ("张三", "zs@corp.example")
    assert p.to == ["me+work@corp.example"] and p.cc == ["boss@corp.example"]
    p = parse(message(
        b'From: "Doe, John" <john@x>',
        b"To: a@x, " + encoded_word("李四", "utf-8") + b" <b@x>, undisclosed-recipients:;",
        b"Cc: Team: c@x, d@x;", body=b"hi"))
    assert p.from_name == "Doe, John"
    assert p.to == ["a@x", "b@x"] and p.cc == ["c@x", "d@x"]


def test_bare_from():
    p = parse(message(b"From: a@x", body=b"hi"))
    assert (p.from_name, p.from_addr, p.to, p.cc, p.subject) == ("", "a@x", [], [], "")


def test_folded_subject_is_one_line():
    p = parse(message(b"From: a@x", b"Subject: first part", b" second part", body=b"hi"))
    assert p.subject == "first part second part"


def test_addressed():
    p = parse(fixture("multipart-alternative.eml"))   # To: Me <me+work@corp.example>, Cc: boss@corp.example
    assert addressed(p, "ME@corp.example") == "to" and addressed(p, "boss@corp.example") == "cc"


def test_addressed_edge_cases():
    p = parse(fixture("multipart-alternative.eml"))
    assert addressed(p, "me+other@corp.example") == "to"
    assert addressed(p, "me+work@Corp.Example") == "to"
    assert addressed(p, "Boss+x@corp.example") == "cc"
    assert addressed(p, "someone@corp.example") == "none"
    assert addressed(p, "me@other.example") == "none"
    assert addressed(p, "+work@corp.example") == "none"
    both = parse(message(b"From: a@x", b"To: me@x", b"Cc: me@x", body=b"hi"))
    assert addressed(both, "me@x") == "to"
    assert addressed(parse(fixture("list-mail.eml")), "other@corp.example") == "none"


# -- never raises ----------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    b"",
    b"\x00\xff\xfe garbage \x81\x40",
    b"Subject:\r\nFrom:\r\nTo:\r\nDate:\r\n\r\n",
    b"From: <<<>>>\r\nTo: a@@b, ,\r\nCc: \xff\r\nMessage-ID: \r\nReferences: <\r\n\r\nx",
    b"Content-Type: multipart/mixed\r\n\r\nno boundary at all \xb9\xa4",
    (b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n--B\r\nContent-Type: text/plain; '
     b"charset=\r\n\r\nx\r\n--B\r\nContent-Type: message/rfc822\r\n\r\nSubject: inner\r\n\r\ny"),
    b"Content-Type: text/plain; charset=gbk\r\nContent-Transfer-Encoding: base64\r\n\r\n!!!!",
    b"Content-Type: text/plain\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\n=E4=B8=",
])
def test_garbage_never_raises(raw):
    p = parse(raw)
    assert isinstance(p.text, str) and isinstance(p.subject, str)
    assert isinstance(p.from_addr, str) and isinstance(p.to, list)
    for att in p.attachments:
        attachment_bytes(raw, att.index)


def test_message_rfc822_part_with_a_name_is_an_attachment():
    raw = message(
        b"From: a@x", b'Content-Type: multipart/mixed; boundary="B"',
        body=b"--B\r\nContent-Type: text/plain\r\n\r\nsee attached\r\n--B\r\n"
        b'Content-Type: message/rfc822; name="forward.eml"\r\n'
        b"Content-Disposition: attachment; filename=forward.eml\r\n\r\n"
        b"Subject: inner\r\nFrom: b@x\r\n\r\ninner body\r\n--B--\r\n")
    p = parse(raw)
    assert p.text == "see attached"
    [att] = p.attachments
    assert att.filename == "forward.eml" and att.content_type == "message/rfc822"
    name, data = attachment_bytes(raw, 0)
    assert name == "forward.eml" and att.size == len(data)
    assert data.startswith(b"Subject: inner\r\nFrom: b@x\r\n\r\ninner body")


def test_message_rfc822_part_with_raw_gbk_header_is_written_out():
    inner = b"Subject: " + "面试".encode("gbk") + b"\r\nFrom: a@x\r\n\r\nbody"
    raw = message(
        b"From: a@x", b'Content-Type: multipart/mixed; boundary="B"',
        body=b"--B\r\nContent-Type: message/rfc822\r\n"
        b"Content-Disposition: attachment; filename=f.eml\r\n\r\n" + inner + b"\r\n--B--\r\n")
    data = attachment_bytes(raw, 0)[1]
    assert parse(data).subject == "面试" and parse(data).text == "body"


# -- which parts are the body ----------------------------------------------------------------


def multipart(kind: str, *parts: bytes) -> bytes:
    """A message whose top part is multipart/<kind>; each part is its headers, a blank line and
    its body (a part may itself be a multipart with its own boundary)."""
    body = b"".join(b"--B\r\n" + part + b"\r\n" for part in parts) + b"--B--\r\n"
    return message(b"From: a@x", b'Content-Type: multipart/' + kind.encode() + b'; boundary="B"',
                   body=body)


def nested(kind: str, boundary: str, *parts: bytes) -> bytes:
    """A part that is a multipart/<kind> with its own boundary."""
    b = boundary.encode()
    body = b"".join(b"--" + b + b"\r\n" + part + b"\r\n" for part in parts) + b"--" + b + b"--"
    return b'Content-Type: multipart/' + kind.encode() + b'; boundary="' + b + b'"\r\n\r\n' + body


HTML_BODY = b"Content-Type: text/html; charset=utf-8\r\n\r\n<p>The real body</p>"
PLAIN_FOOTER = b"Content-Type: text/plain\r\n\r\nYou are on the list. <http://x/unsubscribe>"


def test_html_body_with_a_plain_footer_keeps_both():
    p = parse(multipart("mixed", HTML_BODY, PLAIN_FOOTER))
    assert p.text == "The real body\n\nYou are on the list. <http://x/unsubscribe>"
    p = parse(multipart("mixed", PLAIN_FOOTER, HTML_BODY))
    assert p.text == "You are on the list. <http://x/unsubscribe>\n\nThe real body"


def test_alternatives_are_chosen_where_they_are():
    alternative = nested("alternative", "A", b"Content-Type: text/plain\r\n\r\nplain body", HTML_BODY)
    p = parse(multipart("mixed", alternative, PLAIN_FOOTER))
    assert p.text == "plain body\n\nYou are on the list. <http://x/unsubscribe>"
    related = nested("related", "R", HTML_BODY, b"Content-Type: image/png\r\n\r\nPNG")
    blank_plain = b"Content-Type: text/plain\r\n\r\n \r\n"
    alternative = nested("alternative", "A", blank_plain, related)
    assert parse(multipart("mixed", alternative, PLAIN_FOOTER)).text == (
        "The real body\n\nYou are on the list. <http://x/unsubscribe>")
    assert parse(multipart("alternative", blank_plain, related)).text == "The real body"


def test_alternative_prefers_the_plain_one_that_is_all_plain():
    mixed_html = nested("mixed", "M", HTML_BODY, PLAIN_FOOTER)
    plain = b"Content-Type: text/plain\r\n\r\nthe plain version"
    assert parse(multipart("alternative", mixed_html, plain)).text == "the plain version"
    assert parse(multipart("alternative", plain, mixed_html)).text == "the plain version"
    assert parse(multipart("alternative", mixed_html)).text == (
        "The real body\n\nYou are on the list. <http://x/unsubscribe>")


# -- hostile mail: no exception, no stall ----------------------------------------------------


@pytest.mark.parametrize("header", [b"To", b"Cc", b"From"])
def test_deeply_nested_comments_in_an_address_header(header):
    raw = header + b": a@x " + b"(" * 3000 + b")" * 3000 + b"\r\nSubject: s\r\n\r\nbody"
    p = parse(raw)
    assert p.subject == "s" and p.text == "body"


def test_long_hostile_header_values_are_bounded():
    start = time.monotonic()
    p = parse(b"To: " + b'"' * 50_000 + b"\r\nContent-Type: text/plain; " + b"a=b; " * 20_000
              + b"\r\nSubject: s\r\n\r\nbody")
    assert p.subject == "s" and time.monotonic() - start < 5
    start = time.monotonic()
    refs = b" ".join(b"<r%d@x>" % i for i in range(50_000))
    p = parse(b"References: " + refs + b"\r\nMessage-ID: <m@x>\r\n\r\nbody")
    assert len(p.references) == 50_000 and p.references[-1] == "<r49999@x>"
    assert p.message_id == "<m@x>" and time.monotonic() - start < 5
    # a long list of recipients is cut between two addresses
    many = b", ".join(b"u%d@x.example" % i for i in range(3000))
    p = parse(b"To: " + many + b"\r\n\r\nbody")
    assert p.to[0] == "u0@x.example" and 100 < len(p.to) < 3000
    assert all(re.fullmatch(r"u\d+@x\.example", address) for address in p.to)


@pytest.mark.parametrize("junk", [b"<![ x>", b"<![foo]>", b"<![CDATA[x]]>", b"<?php x ?>", b"<!x>"])
def test_html_declarations_do_not_raise(junk):
    # (the stdlib's HTMLParser raises AssertionError on the first two on Python 3.12.10)
    assert html_text("<p>before</p>" + junk.decode() + "<p>after</p>") == "before\n\nafter"


def test_unclosed_html_constructs_end_at_the_end():
    assert html_text("<p>before</p><!DOCTYPE") == "before"
    assert html_text("<p>before</p><!-- never closed <p>x") == "before"
    assert html_text("<p>before</p><b class=") == "before"
    assert html_text("before<script>never closed</p> after") == "before"
    assert html_text("<title>t</title>x") == "x"
    assert html_text("<title>no end<p>body") == "no end\n\nbody"  # an unclosed title hides nothing


@pytest.mark.parametrize("unit", [
    b"<!--", b"<a ", b"<", b"<![CDATA[", b"<script>", b"<style>", b"<title>", b"&#", b"<p>x",
    b"<a b=\"", b"</", b"<!",
])
def test_html_conversion_takes_linear_time(unit):
    # 25 s for "<!--" x 80 000 and minutes for "<a " x 100 000 with HTMLParser on Python 3.12.10
    html = unit * (400_000 // len(unit))
    start = time.monotonic()
    parse(message(b"From: a@x", b"Content-Type: text/html", body=html))
    assert time.monotonic() - start < 5


def test_html_conversion_details():
    assert html_text("<P>One</P><SCRIPT>x</SCRIPT>two") == "One\n\ntwo"
    assert html_text("a<script>if (x<y) { w(\"</p><td>\"); }</script> b") == "a b"
    assert html_text("a<script>x</SCRIPT >b") == "ab"
    assert html_text("a<style>p > b { }</style>b<style/>c") == "abc"
    assert html_text("a<!-- <b>x</b> -->b<!--[if mso]><p>no</p><![endif]-->c") == "abc"
    assert html_text("1 < 2 and 3<4 &lt;b&gt; &amp; &#x4e2d;") == "1 < 2 and 3<4 <b> & 中"
    assert html_text("<a href='x'>link</a> and <img src=y alt=z>text") == "link and text"


def test_an_unexpected_error_is_still_not_raised(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    raw = fixture("rfc2231-attachment.eml")
    monkeypatch.setattr(mm, "_body_text", boom)
    assert parse(raw) == mm.Parsed()
    monkeypatch.undo()
    monkeypatch.setattr(mm, "_load", boom)
    assert parse(raw) == mm.Parsed()
    with pytest.raises(MailError, match="cannot be read"):
        attachment_bytes(raw, 0)
    monkeypatch.undo()
    with pytest.raises(MailError, match="no attachment"):
        attachment_bytes(raw, 5)


def test_truncated_parameter_keeps_the_body_readable():
    # the stdlib's header parser raises IndexError on a parameter without "="
    raw = message(b"From: a@x", b"Subject: cut", b"Content-Type: text/plain; charset",
                  body="本月工资".encode("gbk"))
    p = parse(raw)
    assert p.subject == "cut" and p.text == "本月工资"
    raw = with_attachment(b"Content-Type: application/pdf; name",
                          b"Content-Disposition: attachment; filename*0*=\x00gbk''%B9")
    assert [a.filename for a in parse(raw).attachments] == ["attachment"]
    assert attachment_bytes(raw, 0)[1] == b"%PDF-1.4"
    # (Python 3.12 raises TypeError in the continuation sorter for a "filename*" with no number)
    raw = with_attachment(b"Content-Type: application/pdf",
                          b"Content-Disposition: attachment; filename*0*=gbk''%B9; filename*")
    assert attachment_bytes(raw, 0)[1] == b"%PDF-1.4"


def test_deeply_nested_multipart_keeps_the_headers():
    nest = "".join(f'Content-Type: multipart/mixed; boundary="b{i}"\r\n\r\n--b{i}\r\n'
                   for i in range(3000))
    raw = f"From: a@x\r\nSubject: nested\r\n{nest}Content-Type: text/plain\r\n\r\nhi\r\n".encode()
    p = parse(raw)
    assert p.subject == "nested" and p.from_addr == "a@x"
    assert p.text == "" and p.attachments == []


def test_damaged_fixtures_never_raise():
    import random

    rng = random.Random(20261014)
    for path in sorted(FIXTURES.glob("*.eml")):
        original = path.read_bytes()
        for _ in range(150):
            data = bytearray(original)
            for _ in range(rng.randint(1, 8)):
                where = rng.randrange(len(data))
                kind = rng.choice(("flip", "cut", "drop", "dup"))
                if kind == "flip":
                    data[where] = rng.randrange(256)
                elif kind == "cut":
                    del data[where:]
                elif kind == "drop":
                    del data[where:where + rng.randint(1, 20)]
                else:
                    data[where:where] = data[max(0, where - 30):where]
                if not data:
                    break
            p = parse(bytes(data))
            for att in p.attachments:
                attachment_bytes(bytes(data), att.index)

import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from ledgerkit.cli import main
from ledgerkit.settings import LedgerError
from ledgerkit.table import decode, read_table

ROOT = Path(__file__).parents[1]
LK = ROOT / "ledger" / "scripts" / "lk.py"
CCB_XLS = ROOT / "tests" / "fixtures" / "deb" / "ccb" / "交易明细_xxxx_2025xxxx_2025xxxx.xls"

# --- write_minimal_pdf: a one-page PDF made of raw objects --------------------------------------

# The padding string of the PDF standard security handler.
PDF_PAD = bytes.fromhex(
    "28bf4e5e4e758a4164004e56fffa01082e2e00b6d0683e802f0ca9fe6453697a")


def rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) % 256
        s[i], s[j] = s[j], s[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        out.append(byte ^ s[(s[i] + s[j]) % 256])
    return bytes(out)


def pdf_text(text: str) -> bytes:
    assert text.isascii(), "Helvetica without an encoding takes ASCII only"
    return b"(" + text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode() + b")"


def build_pdf(content: bytes, password: str | None = None) -> bytes:
    """A one-page PDF with `content` as its page stream and Helvetica as font /F1.

    With a password the file is encrypted (RC4, 40 bit) and needs that user password.
    """
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
         b" /Resources << /Font << /F1 5 0 R >> >> >>"),
        None,  # the content stream, below
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    doc_id = hashlib.md5(content).digest()
    trailer = b"/ID [<%s> <%s>]" % (doc_id.hex().encode(), doc_id.hex().encode())
    if password is not None:
        pad = (password.encode() + PDF_PAD)[:32]
        owner = rc4(hashlib.md5((b"owner" + PDF_PAD)[:32]).digest()[:5], pad)
        key = hashlib.md5(pad + owner + struct.pack("<i", -4) + doc_id).digest()[:5]
        user = rc4(key, PDF_PAD)
        # the key of object 4: md5(key + object number (3 bytes) + generation (2 bytes))
        content = rc4(hashlib.md5(key + struct.pack("<I", 4)[:3] + b"\0\0").digest()[:10], content)
        objects.append(
            b"<< /Filter /Standard /V 1 /R 2 /Length 40 /P -4 /O <%s> /U <%s> >>"
            % (owner.hex().encode(), user.hex().encode()))
        trailer += b" /Encrypt 6 0 R"
    objects[3] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content)

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R %s >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, trailer, xref)
    return bytes(out)


def write_minimal_pdf(path: Path, lines: list[str], *, password: str | None = None) -> None:
    """Write a one-page PDF with one line of Helvetica text per string."""
    content = b"BT /F1 12 Tf 14 TL 72 720 Td\n"
    content += b"\n".join(pdf_text(line) + b" Tj T*" for line in lines) + b"\nET"
    path.write_bytes(build_pdf(content, password))


def write_grid_pdf(path: Path, rows: list[list[str]]) -> None:
    """Write a one-page PDF with a ruled table: one rectangle per cell, so it has table lines."""
    content = b""
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            x, y = 72 + c * 120, 700 - r * 24
            content += b"%d %d 120 24 re S\n" % (x, y)
            content += b"BT /F1 10 Tf %d %d Td %s Tj ET\n" % (x + 6, y + 8, pdf_text(text))
    path.write_bytes(build_pdf(content))


def test_minimal_pdf_xref_offsets(tmp_path):
    p = tmp_path / "x.pdf"
    write_minimal_pdf(p, ["a", "b"])
    data = p.read_bytes()
    start = int(re.search(rb"startxref\n(\d+)\n", data).group(1))
    assert data[start:start + 4] == b"xref"
    entries = re.findall(rb"(\d{10}) 00000 n \n", data[start:])
    assert len(entries) == 5
    for number, offset in enumerate(entries, start=1):
        assert data[int(offset):].startswith(b"%d 0 obj\n" % number)


# --- decode and csv ------------------------------------------------------------------------------


def test_gbk_csv(tmp_path):
    p = tmp_path / "a.csv"
    p.write_bytes("交易时间,金额\n2026-09-01,20.00\n".encode("gbk"))
    assert read_table(p) == [["交易时间", "金额"], ["2026-09-01", "20.00"]]


def test_bom_crlf_csv(tmp_path):
    p = tmp_path / "m.csv"
    p.write_bytes("\ufeff交易创建时间,实付金额\r\n2026-09-01,¥20.00\r\n".encode())
    assert read_table(p)[1] == ["2026-09-01", "¥20.00"]
    assert read_table(p)[0] == ["交易创建时间", "实付金额"]  # no BOM left in the first cell


def test_decode():
    assert decode("\ufeff记账日\r\n".encode()) == "记账日\n"
    assert decode("记账日\r\n".encode("gbk")) == "记账日\n"
    assert decode("记账日".encode("gb18030")) == "记账日"
    assert decode(b"") == ""
    assert decode(b"plain,ascii\r\n") == "plain,ascii\n"


def test_decode_refuses_bytes_neither_utf8_nor_gbk():
    with pytest.raises(LedgerError, match="UTF-8"):
        decode(b"a,b\n\x81")


def test_csv_quotes_and_embedded_newlines(tmp_path):
    p = tmp_path / "q.csv"
    p.write_bytes('商户,备注\r\n"a, b","line1\r\nline2"\r\n'.encode("gbk"))
    assert read_table(p) == [["商户", "备注"], ["a, b", "line1\nline2"]]


def test_csv_drops_empty_rows_and_keeps_cells(tmp_path):
    p = tmp_path / "e.txt"
    p.write_text("a,,\n,,\n\n b ,c,\n  , ,\n", "utf-8")
    assert read_table(p) == [["a", "", ""], [" b ", "c", ""]]


def test_suffix_is_case_insensitive(tmp_path):
    p = tmp_path / "A.CSV"
    p.write_text("x,y\n", "utf-8")
    assert read_table(p) == [["x", "y"]]


def test_unknown_suffix(tmp_path):
    p = tmp_path / "a.docx"
    p.write_bytes(b"PK")
    with pytest.raises(LedgerError, match=r"\.docx"):
        read_table(p)


def test_missing_file(tmp_path):
    with pytest.raises(LedgerError, match="no such file"):
        read_table(tmp_path / "nothing.csv")


# --- xlsx and xls ---------------------------------------------------------------------------------


def write_xlsx(path: Path) -> None:
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "first"
    sheet.append(["日期", "金额", "备注"])
    sheet.append(["2026-09-01", 20.5, None])
    sheet.append([None, None, None])
    sheet.append([datetime(2026, 9, 2, 8, 30), 7, "早餐"])
    sheet.append([None, 3.0, None])
    book.create_sheet("second").append(["x", "y"])
    book.save(path)


def test_xlsx(tmp_path):
    p = tmp_path / "b.xlsx"
    write_xlsx(p)
    assert read_table(p) == [
        ["日期", "金额", "备注"],
        ["2026-09-01", "20.5", ""],
        ["2026-09-02 08:30:00", "7", "早餐"],
        ["", "3", ""],
    ]


def test_xlsx_sheet(tmp_path):
    p = tmp_path / "b.xlsx"
    write_xlsx(p)
    assert read_table(p, sheet=1) == [["x", "y"]]
    for bad in (2, -1):
        with pytest.raises(LedgerError, match="sheet"):
            read_table(p, sheet=bad)


def test_xlsx_with_a_wrong_dimension(tmp_path):
    """Some exporters write <dimension ref="A1"/>; read-only mode would stop after one cell."""
    good, bad = tmp_path / "good.xlsx", tmp_path / "bad.xlsx"
    write_xlsx(good)
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(bad, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                data, count = re.subn(rb'<dimension ref="A1:C5"\s*/>', b'<dimension ref="A1"/>', data)
                assert count == 1
            dst.writestr(item, data)
    assert read_table(bad) == read_table(good)


def test_xlsx_file_is_closed_after_reading(tmp_path):
    p = tmp_path / "b.xlsx"
    write_xlsx(p)
    read_table(p)
    p.unlink()  # fails on Windows while a handle is open
    assert not p.exists()


def test_xlsx_that_is_not_one(tmp_path):
    p = tmp_path / "b.xlsx"
    p.write_text("a,b\n", "utf-8")
    with pytest.raises(LedgerError, match=r"b\.xlsx"):
        read_table(p)


def test_xls_fixture():
    rows = read_table(CCB_XLS)
    assert any("记账日" in cell for row in rows for cell in row)
    # the 20.00 amount is a float in the file; it reads as a number without ".0"
    assert any(row[3] == "20" for row in rows)
    assert any(row[3] == "16.78" for row in rows)


def test_xls_sheet_out_of_range():
    with pytest.raises(LedgerError, match="sheet"):
        read_table(CCB_XLS, sheet=1)


def test_xls_that_is_not_one(tmp_path):
    p = tmp_path / "b.xls"
    p.write_text("<html></html>", "utf-8")
    with pytest.raises(LedgerError, match=r"b\.xls"):
        read_table(p)


# --- pdf ------------------------------------------------------------------------------------------


def test_pdf_text(tmp_path):
    p = tmp_path / "s.pdf"
    write_minimal_pdf(p, ["2026-09-01 20.00"])
    assert ["2026-09-01 20.00"] in read_table(p)


def test_pdf_text_lines_are_rows(tmp_path):
    p = tmp_path / "s.pdf"
    write_minimal_pdf(p, ["Statement (Sept) a\\b", "2026-09-01 20.00", "2026-09-02 7.50"])
    assert read_table(p) == [
        ["Statement (Sept) a\\b"], ["2026-09-01 20.00"], ["2026-09-02 7.50"]]


def test_pdf_table(tmp_path):
    p = tmp_path / "t.pdf"
    write_grid_pdf(p, [["Date", "Amount"], ["2026-09-01", "20.00"], ["2026-09-02", "7.50"]])
    assert read_table(p) == [["Date", "Amount"], ["2026-09-01", "20.00"], ["2026-09-02", "7.50"]]


def test_pdf_password(tmp_path):
    p = tmp_path / "s.pdf"
    write_minimal_pdf(p, ["2026-09-01 20.00"], password="s3cret")
    assert read_table(p, password="s3cret") == [["2026-09-01 20.00"]]
    with pytest.raises(LedgerError, match="password") as needs:
        read_table(p)
    assert "s3cret" not in str(needs.value)
    with pytest.raises(LedgerError, match="password") as wrong:
        read_table(p, password="nope")
    assert "nope" not in str(wrong.value)
    p.unlink()  # a failed read leaves no handle on the file (Windows would refuse)
    assert not p.exists()


def test_pdf_password_is_ignored_when_not_needed(tmp_path):
    p = tmp_path / "s.pdf"
    write_minimal_pdf(p, ["2026-09-01 20.00"])
    assert read_table(p, password="unneeded") == [["2026-09-01 20.00"]]


def test_pdf_that_is_not_one(tmp_path):
    p = tmp_path / "s.pdf"
    p.write_text("not a pdf", "utf-8")
    with pytest.raises(LedgerError, match=r"s\.pdf"):
        read_table(p)


# --- paths with spaces and Chinese ---------------------------------------------------------------


def odd_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "账单 备份" / "2026 09"
    folder.mkdir(parents=True)
    return folder


def test_paths_with_spaces_and_chinese(tmp_path):
    folder = odd_dir(tmp_path)
    csv_path = folder / "交易明细 2026-09.csv"
    csv_path.write_bytes("交易时间,金额\n2026-09-01,20.00\n".encode("gbk"))
    xls_path = folder / "交易明细 2026-09.xls"
    shutil.copyfile(CCB_XLS, xls_path)
    xlsx_path = folder / "交易明细 2026-09.xlsx"
    write_xlsx(xlsx_path)
    pdf_path = folder / "交易明细 2026-09.pdf"
    write_minimal_pdf(pdf_path, ["2026-09-01 20.00"])

    assert read_table(csv_path)[1] == ["2026-09-01", "20.00"]
    assert any("记账日" in cell for row in read_table(xls_path) for cell in row)
    assert read_table(xlsx_path)[0] == ["日期", "金额", "备注"]
    assert read_table(pdf_path) == [["2026-09-01 20.00"]]


# --- the command ----------------------------------------------------------------------------------


def test_cli_prints_csv(tmp_path, capsys):
    p = tmp_path / "a.csv"
    p.write_bytes('交易时间,金额,备注\n2026-09-01,20.00,"a, ""b"""\n'.encode("gbk"))
    assert main(["table", str(p)]) == 0
    out = capsys.readouterr().out
    assert out == '交易时间,金额,备注\n2026-09-01,20.00,"a, ""b"""\n'


def test_cli_sheet(tmp_path, capsys):
    p = tmp_path / "b.xlsx"
    write_xlsx(p)
    assert main(["table", str(p), "--sheet", "1"]) == 0
    assert capsys.readouterr().out == "x,y\n"


def test_cli_password(tmp_path, capsys):
    p = tmp_path / "s.pdf"
    write_minimal_pdf(p, ["2026-09-01 20.00"], password="s3cret")
    assert main(["table", str(p), "--password", "s3cret"]) == 0
    assert capsys.readouterr().out == "2026-09-01 20.00\n"


@pytest.mark.parametrize("args", [["--password", "nope"], []])
def test_cli_password_is_never_printed(tmp_path, capsys, args):
    p = tmp_path / "s.pdf"
    write_minimal_pdf(p, ["2026-09-01 20.00"], password="s3cret")
    assert main(["table", str(p), *args]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("lk: ")
    assert "password" in captured.err
    assert "nope" not in captured.err + captured.out
    assert "s3cret" not in captured.err + captured.out


def test_cli_error(tmp_path, capsys):
    p = tmp_path / "a.docx"
    p.write_bytes(b"PK")
    assert main(["table", str(p)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("lk: ") and ".docx" in captured.err


def test_cli_writes_nothing_beside_the_bill(tmp_path, capsys):
    p = tmp_path / "s.pdf"
    write_minimal_pdf(p, ["2026-09-01 20.00"], password="s3cret")
    before = sorted(os.listdir(tmp_path))
    assert main(["table", str(p), "--password", "s3cret"]) == 0
    assert sorted(os.listdir(tmp_path)) == before


def run_lk(*args: str) -> subprocess.CompletedProcess:
    """Run lk.py like the agent does, with stdout a pipe and no encoding hints in the environment."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    return subprocess.run(
        [sys.executable, str(LK), *args], capture_output=True, env=env, timeout=120, check=False)


def test_lk_prints_utf8_whatever_the_console_code_page(tmp_path):
    folder = odd_dir(tmp_path)
    gbk_csv = folder / "支付宝 交易明细.csv"
    gbk_csv.write_bytes("交易时间,金额\n2026-09-01,20.00\n".encode("gbk"))
    xls = folder / "交易明细 2026-09.xls"
    shutil.copyfile(CCB_XLS, xls)

    done = run_lk("table", str(gbk_csv))
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    assert done.stdout.decode("utf-8") == "交易时间,金额\n2026-09-01,20.00\n".replace("\n", os.linesep)

    done = run_lk("table", str(xls))
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    assert "记账日" in done.stdout.decode("utf-8")

    done = run_lk("table", str(folder / "nothing.csv"))
    assert done.returncode == 1
    assert done.stderr.decode("utf-8").startswith("lk: no such file")


def test_table_module_imports_no_heavy_libraries():
    """lk's other commands should not pay for pdfplumber or xlrd at start-up."""
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import ledgerkit.cli, ledgerkit.table;"
        "print(sorted(m for m in ('pdfplumber', 'openpyxl', 'xlrd') if m in sys.modules))")
    done = subprocess.run(
        [sys.executable, "-c", code, str(ROOT / "ledger" / "scripts")],
        capture_output=True, text=True, check=True, timeout=120)
    assert done.stdout.strip() == "[]"

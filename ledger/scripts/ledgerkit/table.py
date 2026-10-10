"""Read a bill as text: the rows of a csv, txt, xlsx, xls or pdf file.

`lk table` prints them as CSV, so the agent can read banks the converter does not support and
write the entries itself. The reading libraries are imported when a file of their kind is read,
so the other commands do not pay for them.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from .settings import LedgerError

SUFFIXES = (".csv", ".txt", ".xlsx", ".xls", ".pdf")


def decode(data: bytes) -> str:
    """The text of a CSV file: UTF-8 (a BOM is dropped), else GBK/GB18030. No `\\r` is left."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("gb18030")
        except UnicodeDecodeError as err:
            raise LedgerError(f"the file is neither UTF-8 nor GBK text: {err}") from err
    return text.replace("\r", "")


def cell_text(value) -> str:
    """A spreadsheet or PDF cell as text; empty is "".

    A whole number stored as a float (every number in an .xls) reads without ".0".
    """
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return str(value)


def drop_empty_rows(rows: list[list[str]]) -> list[list[str]]:
    """The rows with something in them. Cells stay as read."""
    return [row for row in rows if any(cell.strip() for cell in row)]


def check_sheet(sheet: int, count: int, path: Path) -> None:
    if not 0 <= sheet < count:
        raise LedgerError(
            f"{path.name} has {count} sheet(s); there is no sheet {sheet} (they count from 0)")


def read_csv(path: Path) -> list[list[str]]:
    text = decode(path.read_bytes())
    try:
        return list(csv.reader(io.StringIO(text)))
    except csv.Error as err:
        raise LedgerError(f"cannot read {path.name} as CSV: {err}") from err


def read_xlsx(path: Path, sheet: int) -> list[list[str]]:
    import openpyxl

    try:
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as err:  # openpyxl raises zipfile, XML and its own errors for a bad file
        raise LedgerError(f"cannot read {path.name} as an xlsx workbook: {err}") from err
    try:
        check_sheet(sheet, len(book.worksheets), path)
        worksheet = book.worksheets[sheet]
        # The dimension some exporters write (A1) would end the read after one cell.
        worksheet.reset_dimensions()
        rows = [[cell_text(v) for v in row] for row in worksheet.iter_rows(values_only=True)]
        # Without the dimension rows end at their last cell; make the table rectangular again.
        width = max((len(row) for row in rows), default=0)
        return [row + [""] * (width - len(row)) for row in rows]
    finally:
        book.close()


def xls_cell(cell, datemode: int) -> str:
    import xlrd

    kind, value = cell.ctype, cell.value
    if kind in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return ""
    if kind == xlrd.XL_CELL_DATE:
        try:
            moment = xlrd.xldate_as_datetime(value, datemode)
        except xlrd.XLDateError:
            return cell_text(value)
        return str(moment.time() if value < 1 else moment)
    if kind == xlrd.XL_CELL_BOOLEAN:
        return str(bool(value))
    if kind == xlrd.XL_CELL_ERROR:
        return xlrd.error_text_from_code.get(value, "")
    return cell_text(value)


def read_xls(path: Path, sheet: int) -> list[list[str]]:
    import xlrd

    try:
        book = xlrd.open_workbook(str(path))
    except Exception as err:  # xlrd raises its own, struct and OS errors for a bad file
        raise LedgerError(f"cannot read {path.name} as an xls workbook: {err}") from err
    try:
        check_sheet(sheet, book.nsheets, path)
        return [
            [xls_cell(cell, book.datemode) for cell in row]
            for row in book.sheet_by_index(sheet).get_rows()
        ]
    finally:
        book.release_resources()


def read_pdf(path: Path, password: str | None) -> list[list[str]]:
    import pdfplumber
    from pdfminer.pdfdocument import PDFPasswordIncorrect

    rows: list[list[str]] = []
    try:
        with pdfplumber.open(path, password=password or None) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                if tables:
                    rows.extend([cell_text(c) for c in row] for t in tables for row in t)
                else:
                    rows.extend([line] for line in (page.extract_text() or "").split("\n"))
    except Exception as err:  # pdfplumber wraps what pdfminer raised; OS errors come as they are
        inner = err.args[0] if err.args else err
        if isinstance(inner, PDFPasswordIncorrect):
            why = "is wrong" if password else "is needed (--password)"
            raise LedgerError(f"cannot open {path.name}: the password {why}") from err
        detail = str(inner) or type(inner).__name__
        raise LedgerError(f"cannot read {path.name} as a PDF: {detail}") from err
    return rows


def read_table(
    path: Path, *, sheet: int = 0, password: str | None = None
) -> list[list[str]]:
    """The rows of a bill, every cell as text.

    A csv or txt file is decoded with `decode`. A spreadsheet gives the rows of one sheet
    (`sheet` counts from 0). A PDF gives every table of every page, or the text lines of a page
    without a table as one-cell rows. `password` opens an encrypted PDF and is used for nothing
    else. Rows with nothing in them are dropped.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUFFIXES:
        raise LedgerError(
            f"cannot read {path.name}: {suffix or 'no suffix'} is not one of {', '.join(SUFFIXES)}")
    if not path.is_file():
        raise LedgerError(f"no such file: {path}")
    if suffix in (".csv", ".txt"):
        rows = read_csv(path)
    elif suffix == ".xlsx":
        rows = read_xlsx(path, sheet)
    elif suffix == ".xls":
        rows = read_xls(path, sheet)
    else:
        rows = read_pdf(path, password)
    return drop_empty_rows(rows)

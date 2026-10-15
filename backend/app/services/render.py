"""Bounded, dependency-light artifact rendering for CSV, Excel and PDF exports.

Every format renders the SAME already-scoped rows: rendering never queries and
never widens authorization. Text cells pass through the CSV-injection guard so
a spreadsheet formula in a policy number cannot execute when a report is opened.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal

from app.services.complaints import safe_csv_cell

FORMATS = ("csv", "xlsx", "pdf")
MEDIA_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}
EXTENSIONS = {"csv": "csv", "xlsx": "xlsx", "pdf": "pdf"}

Cell = str | int | float | Decimal | datetime | bool | None


def _text(value: Cell) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    return safe_csv_cell(str(value))


def _xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _column_name(index: int) -> str:
    name = ""
    while index >= 0:
        name = chr(ord("A") + index % 26) + name
        index = index // 26 - 1
    return name


def csv_bytes(headers: Sequence[str], rows: Iterable[Sequence[Cell]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([_text(cell) for cell in row])
    return buffer.getvalue().encode("utf-8")


def xlsx_bytes(sheet: str, headers: Sequence[str], rows: Iterable[Sequence[Cell]]) -> bytes:
    """Minimal single-sheet OOXML workbook; numbers stay numeric for pivoting."""
    body: list[str] = []
    all_rows: list[Sequence[Cell]] = [list(headers), *rows]
    for row_index, row in enumerate(all_rows, start=1):
        cells = []
        for column_index, cell in enumerate(row):
            reference = f"{_column_name(column_index)}{row_index}"
            if isinstance(cell, bool) or cell is None:
                cells.append(f'<c r="{reference}" t="inlineStr"><is><t>{_text(cell)}</t></is></c>')
            elif isinstance(cell, (int, float, Decimal)):
                cells.append(f'<c r="{reference}"><v>{cell}</v></c>')
            else:
                value = _xml(_text(cell))
                cells.append(f'<c r="{reference}" t="inlineStr"><is><t>{value}</t></is></c>')
        body.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(body)}</sheetData></worksheet>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{_xml(sheet[:31])}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    rel_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
    relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{rel_type}" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    rels_type = "application/vnd.openxmlformats-package.relationships+xml"
    sheet_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
    worksheet_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        f'<Default Extension="rels" ContentType="{rels_type}"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/xl/workbook.xml" ContentType="{sheet_type}"/>'
        f'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="{worksheet_type}"/>'
        "</Types>"
    )
    document_type = (
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{document_type}" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    return buffer.getvalue()


def _pdf_escape(value: str) -> str:
    # Base-14 Helvetica is single-byte; non-Latin glyphs are replaced rather
    # than silently dropped, so a reader still sees the row is present.
    latin = value.encode("latin-1", "replace").decode("latin-1")
    return latin.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def pdf_bytes(title: str, headers: Sequence[str], rows: Iterable[Sequence[Cell]]) -> bytes:
    """Minimal, dependency-free PDF table: A4 landscape, 30 rows per page."""
    lines_per_page = 30
    column_count = max(len(headers), 1)
    per_column = max(int(780 / column_count), 12)
    header_line = f"{title} ({datetime.now().isoformat(timespec='seconds')})"

    def page_objects(page_rows: list[Sequence[Cell]], page_number: int) -> str:
        parts = [
            "BT /F1 11 Tf 30 560 Td (" + _pdf_escape(header_line) + ") Tj ET",
            "BT /F1 9 Tf 30 542 Td (" + _pdf_escape(" | ".join(headers)[:160]) + ") Tj ET",
        ]
        offset = 522
        for row in page_rows:
            text = " | ".join(_text(cell)[:per_column] for cell in row)[:220]
            parts.append(f"BT /F1 8 Tf 30 {offset} Td (" + _pdf_escape(text) + ") Tj ET")
            offset -= 16
        parts.append(f"BT /F1 8 Tf 30 30 Td (page {page_number}) Tj ET")
        return "\n".join(parts)

    pages: list[str] = []
    buffered: list[Sequence[Cell]] = []
    for row in rows:
        buffered.append(row)
        if len(buffered) == lines_per_page:
            pages.append(page_objects(buffered, len(pages) + 1))
            buffered = []
    if buffered or not pages:
        pages.append(page_objects(buffered, len(pages) + 1))

    objects: list[str] = []
    page_ids = [4 + index * 2 for index in range(len(pages))]
    objects.append("<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>")
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for index, content in enumerate(pages):
        page_id = page_ids[index]
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 842 595] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>"
        )
        objects.append(f"<< /Length {len(content)} >>\nstream\n{content}\nendstream")

    output = io.BytesIO()
    output.write(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(output.tell())
        output.write(f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1"))
    xref = output.tell()
    output.write(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
    output.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.write(f"{offset:010d} 00000 n \n".encode("latin-1"))
    trailer = f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n"
    output.write(trailer.encode("latin-1"))
    output.write(b"%%EOF\n")
    return output.getvalue()


def tabular_bytes(
    fmt: str, sheet: str, headers: Sequence[str], rows: Sequence[Sequence[Cell]]
) -> bytes:
    if fmt == "csv":
        return csv_bytes(headers, rows)
    if fmt == "xlsx":
        return xlsx_bytes(sheet, headers, rows)
    if fmt == "pdf":
        return pdf_bytes(sheet, headers, rows)
    raise ValueError("Unsupported export format")

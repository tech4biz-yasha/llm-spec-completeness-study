"""A small, dependency-free PDF 1.4 writer.

rules.yaml#EXIT-09 requires the NOC to be a PDF and says nothing about its
layout; the module needs to emit a valid file without pulling a rendering stack
into the service image. Output is byte-for-byte deterministic for the same
inputs, which matters because the stored digest is what proves the document was
not altered.

Text is encoded WinAnsi (Latin-1). Characters outside it — Arabic, for one — are
not representable by the standard Type1 base fonts used here; :func:`render_pdf`
raises rather than emitting mojibake into a legal document. An Arabic NOC needs
an embedded font, and the kit does not specify one (blockers.md#B-10).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

PAGE_WIDTH: Final[int] = 595  # A4 at 72 dpi
PAGE_HEIGHT: Final[int] = 842
MARGIN: Final[int] = 56
TITLE_SIZE: Final[int] = 16
BODY_SIZE: Final[int] = 11
LEADING: Final[int] = 17


class PdfRenderError(ValueError):
    """The content cannot be represented in the PDF being produced."""


def _escape(text: str) -> bytes:
    try:
        raw = text.encode("cp1252")
    except UnicodeEncodeError as exc:
        raise PdfRenderError(
            f"character {text[exc.start:exc.end]!r} cannot be encoded with the WinAnsi "
            "base font; an embedded font is required (blockers.md#B-10)"
        ) from None
    out = bytearray()
    for byte in raw:
        if byte in (0x28, 0x29, 0x5C):  # ( ) \
            out.append(0x5C)
        out.append(byte)
    return bytes(out)


def _pdf_date(moment: datetime) -> str:
    offset = moment.utcoffset()
    if offset is None:
        raise PdfRenderError("PDF timestamps require a timezone-aware datetime")
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"D:{moment:%Y%m%d%H%M%S}{sign}{hours:02d}'{minutes:02d}'"


def _content_stream(title: str, lines: list[str]) -> bytes:
    y = PAGE_HEIGHT - MARGIN
    parts = [b"BT\n"]
    parts.append(f"/F2 {TITLE_SIZE} Tf\n".encode("ascii"))
    parts.append(f"1 0 0 1 {MARGIN} {y} Tm\n".encode("ascii"))
    parts.append(b"(" + _escape(title) + b") Tj\n")

    y -= LEADING * 2
    parts.append(f"/F1 {BODY_SIZE} Tf\n".encode("ascii"))
    parts.append(f"{LEADING} TL\n".encode("ascii"))
    parts.append(f"1 0 0 1 {MARGIN} {y} Tm\n".encode("ascii"))
    for index, line in enumerate(lines):
        if index:
            parts.append(b"T*\n")
        parts.append(b"(" + _escape(line) + b") Tj\n")
    parts.append(b"ET\n")
    return b"".join(parts)


def render_pdf(
    *,
    title: str,
    lines: list[str],
    subject: str,
    created_at: datetime,
    producer: str = "Meridian exit workflow module",
) -> bytes:
    """Render a single-page A4 PDF.

    :param created_at: timezone-aware; written to ``/CreationDate`` as given, so
        pass the Asia/Dubai issue time for a document a UAE reader will open.
    """
    if len(lines) > (PAGE_HEIGHT - 2 * MARGIN) // LEADING:
        raise PdfRenderError("content exceeds one page; multi-page output is not implemented")

    content = _content_stream(title, lines)

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            "/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> /Contents 4 0 R >>"
        ).encode("ascii"),
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        (
            b"<< /Title (" + _escape(title) + b") /Subject (" + _escape(subject) + b") "
            b"/Producer (" + _escape(producer) + b") "
            b"/CreationDate (" + _escape(_pdf_date(created_at)) + b") >>"
        ),
    ]

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    xref_offset = len(out)
    count = len(objects) + 1
    out += f"xref\n0 {count}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {count} /Root 1 0 R /Info {len(objects)} 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)

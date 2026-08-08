"""A minimal, dependency-free PDF writer for the Exit NOC.

Deliberately tiny: a single A4 page of Helvetica text plus rules. It exists so
the NOC is a real, downloadable PDF without pulling a reporting engine into a
payments-critical path. Swap :func:`render_pdf` for a template engine if the
certificate ever needs logos or Arabic text (Type1 WinAnsi cannot render
Arabic).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

PAGE_WIDTH = 595  # A4 @ 72dpi
PAGE_HEIGHT = 842
MARGIN = 56


@dataclass(frozen=True, slots=True)
class Line:
    text: str = ""
    size: float = 11.0
    bold: bool = False
    space_after: float = 6.0
    rule: bool = False


def _escape(text: str) -> bytes:
    encoded = text.encode("cp1252", errors="replace")
    out = bytearray()
    for byte in encoded:
        if byte in (0x28, 0x29, 0x5C):  # ( ) \
            out.append(0x5C)
        out.append(byte)
    return bytes(out)


def _content_stream(lines: list[Line]) -> bytes:
    ops = bytearray()
    y = PAGE_HEIGHT - MARGIN
    for line in lines:
        leading = line.size * 1.25 + line.space_after
        if line.rule:
            y -= line.space_after
            ops += (
                f"0.6 w 0.75 0.75 0.75 RG {MARGIN} {y:.2f} m "
                f"{PAGE_WIDTH - MARGIN} {y:.2f} l S\n"
            ).encode("ascii")
            y -= line.space_after
            continue
        if line.text:
            font = "/F2" if line.bold else "/F1"
            ops += b"BT " + f"{font} {line.size:.2f} Tf 0 g ".encode("ascii")
            ops += f"1 0 0 1 {MARGIN} {y:.2f} Tm (".encode("ascii")
            ops += _escape(line.text)
            ops += b") Tj ET\n"
        y -= leading
        if y < MARGIN:  # pragma: no cover - certificates are short by design
            break
    return bytes(ops)


def render_pdf(title: str, lines: list[Line], *, created_at: datetime) -> bytes:
    """Render a one-page PDF. Deterministic for a given input."""

    content = _content_stream(lines)
    stamp = created_at.strftime("D:%Y%m%d%H%M%SZ")

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
            b"<< /Title (" + _escape(title) + b") /Producer (Meridian Exit Workflow) "
            b"/Creator (Meridian Exit Workflow) /CreationDate (" + stamp.encode("ascii") + b") >>"
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

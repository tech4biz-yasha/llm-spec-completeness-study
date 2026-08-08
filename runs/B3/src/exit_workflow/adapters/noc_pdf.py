"""Default NOC renderer. rules.yaml#EXIT-09 — "NOC is a PDF".

Writes a minimal, dependency-free PDF 1.4 containing only facts already recorded on the
workflow. The kit specifies no template, wording, letterhead, signatory or language, so
none are invented: the document is a plain field listing plus the sentence the rule name
itself supplies (a no-objection statement for the named contract). A deployment holding
the approved template supplies its own ``NocRenderer``. See blockers.md#B-8.

Output is deterministic for a given ``NocContext`` — the same workflow renders the same
bytes, which is what makes the stored sha256 a meaningful integrity check.
"""

from __future__ import annotations

from ..clock import BUSINESS_TZ
from ..ports.renderer import NocContext

_PAGE_WIDTH = 595  # A4 at 72 dpi
_PAGE_HEIGHT = 842
_MARGIN = 56
_LEADING = 18


def _escape(text: str) -> bytes:
    """PDF literal-string escaping, encoded as PDFDocEncoding (latin-1 compatible)."""
    encoded = text.encode("latin-1", errors="replace")
    out = bytearray()
    for byte in encoded:
        if byte in (0x28, 0x29, 0x5C):  # ( ) \
            out.append(0x5C)
        out.append(byte)
    return bytes(out)


def _pdf_date(context: NocContext) -> bytes:
    local = context.issued_at.astimezone(BUSINESS_TZ)
    offset = local.utcoffset() or local.tzinfo.utcoffset(local)  # type: ignore[union-attr]
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    stamp = local.strftime("%Y%m%d%H%M%S")
    return f"D:{stamp}{sign}{total_minutes // 60:02d}'{total_minutes % 60:02d}'".encode("latin-1")


class SimpleNocPdfRenderer:
    """Lays the NOC context out as headed text on a single A4 page."""

    def render(self, context: NocContext) -> bytes:
        lines: list[tuple[str, str]] = [
            ("H1", "NO OBJECTION CERTIFICATE"),
            ("SPACE", ""),
            (
                "BODY",
                (
                    "This certificate records that the tenancy exit workflow below has "
                    "completed and that"
                ),
            ),
            (
                "BODY",
                ("the security deposit settlement for the contract has been disbursed in full."),
            ),
            ("SPACE", ""),
            ("FIELD", f"Exit workflow: {context.workflow_id}"),
            ("FIELD", f"Contract: {context.contract_id}"),
            ("FIELD", f"Property: {context.property_id}"),
            ("FIELD", f"Tenant: {context.tenant_id}"),
            ("FIELD", f"Owner: {context.owner_id}"),
            ("FIELD", f"Move-out date: {context.move_out_date.isoformat()} (Asia/Dubai)"),
            (
                "FIELD",
                f"Deposit refund: {context.currency} {context.refund_amount:.2f}",
            ),
            ("FIELD", f"Payment: {context.payment_id}"),
            ("FIELD", f"Gateway reference: {context.payment_reference or '-'}"),
            (
                "FIELD",
                "Issued at: "
                + context.issued_at.astimezone(BUSINESS_TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
            ),
        ]

        content = bytearray()
        content += b"BT\n"
        y = _PAGE_HEIGHT - _MARGIN
        for kind, text in lines:
            if kind == "SPACE":
                y -= _LEADING
                continue
            font, size = ("F1", 16) if kind == "H1" else ("F2", 11)
            content += f"/{font} {size} Tf\n".encode("latin-1")
            content += f"1 0 0 1 {_MARGIN} {y} Tm\n".encode("latin-1")
            content += b"(" + _escape(text) + b") Tj\n"
            y -= _LEADING if kind != "H1" else _LEADING + 8
        content += b"ET\n"
        stream = bytes(content)

        objects: list[bytes] = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_PAGE_WIDTH} {_PAGE_HEIGHT}] "
                "/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> /Contents 4 0 R >>"
            ).encode("latin-1"),
            (
                b"<< /Length "
                + str(len(stream)).encode("latin-1")
                + b" >>\nstream\n"
                + stream
                + b"endstream"
            ),
            (
                b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold"
                b" /Encoding /WinAnsiEncoding >>"
            ),
            (b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"),
            (
                b"<< /Title ("
                + _escape(f"NOC {context.workflow_id}")
                + b") /Producer (Meridian exit workflow) /CreationDate ("
                + _pdf_date(context)
                + b") >>"
            ),
        ]

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: list[int] = []
        for number, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{number} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"

        xref_offset = len(out)
        count = len(objects) + 1
        out += f"xref\n0 {count}\n".encode("latin-1")
        out += b"0000000000 65535 f \n"
        for offset in offsets:
            out += f"{offset:010d} 00000 n \n".encode("latin-1")
        out += (
            f"trailer\n<< /Size {count} /Root 1 0 R /Info {len(objects)} 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("latin-1")
        return bytes(out)

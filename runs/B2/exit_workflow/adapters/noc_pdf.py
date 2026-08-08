"""NOC PDF renderer — rules.yaml#EXIT-09 ("NOC is a PDF").

The document prints only facts the specification establishes: the parties, the
tenancy identifiers, the move-out date, the deposit arithmetic and the refund
payment reference. blockers.md#B-010: no NOC template, legal wording, letterhead
or signatory is specified anywhere in the kit, so none is invented here — the
renderer is a port, and a real template can replace this implementation without
touching the workflow.

Output is a self-contained PDF 1.4 file with no external dependency, so the
bytes are deterministic for a given context (which is what makes the stored
sha256 meaningful).
"""

from __future__ import annotations

from ..money import CURRENCY
from ..ports import NocContext

_FONT = "Helvetica"
_TITLE_SIZE = 16
_BODY_SIZE = 10.5
_LEADING = 18
_MARGIN_X = 56
_PAGE_W, _PAGE_H = 595, 842  # A4 points


class NocPdfRenderer:
    """rules.yaml#EXIT-09 — renders the NOC document."""

    def render(self, context: NocContext) -> bytes:
        lines = self._lines(context)
        return _build_pdf(lines)

    @staticmethod
    def _lines(ctx: NocContext) -> list[tuple[str, float]]:
        body: list[tuple[str, float]] = [("NO OBJECTION CERTIFICATE", _TITLE_SIZE), ("", _BODY_SIZE)]
        fields = [
            ("Exit workflow", ctx.workflow_id),
            ("Contract", ctx.contract_id),
            ("Property", ctx.property_id),
            ("Tenant", ctx.tenant_id),
            ("Owner", ctx.owner_id),
            # edges.yaml#X-007 — Dubai calendar day.
            ("Move-out date", ctx.move_out_date.isoformat()),
            ("Security deposit", f"{CURRENCY} {ctx.security_deposit}"),
            ("Confirmed damage", f"{CURRENCY} {ctx.confirmed_damage}"),
            # rules.yaml#EXIT-07 — deposit minus confirmed damage.
            ("Deposit refund", f"{CURRENCY} {ctx.refund_amount}"),
            # rules.yaml#EXIT-08 — the refund is settled before this document exists.
            ("Refund payment reference", ctx.payment_reference),
            ("Issued at (Asia/Dubai)", ctx.issued_at_dubai),
        ]
        body += [(f"{label}: {value}", _BODY_SIZE) for label, value in fields]
        return body


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _build_pdf(lines: list[tuple[str, float]]) -> bytes:
    content_parts = ["BT", f"1 0 0 1 {_MARGIN_X} {_PAGE_H - 90} Tm", f"{_LEADING} TL"]
    for text, size in lines:
        content_parts.append(f"/F1 {size} Tf")
        content_parts.append(f"({_escape(text)}) Tj")
        content_parts.append("T*")
    content_parts.append("ET")
    content = "\n".join(content_parts).encode("latin-1", errors="replace")

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_PAGE_W} {_PAGE_H}] "
            f"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ).encode("ascii"),
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"\nendstream",
        f"<< /Type /Font /Subtype /Type1 /BaseFont /{_FONT} /Encoding /WinAnsiEncoding >>".encode(
            "ascii"
        ),
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, payload in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + payload + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)

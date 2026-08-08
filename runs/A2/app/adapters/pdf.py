"""A dependency-free PDF writer.

Rationale: the NOC is a legal artefact that must render identically for the seven years
we are required to retain it. Pulling in a heavyweight rendering stack (or worse, a
headless browser) for one single-page certificate adds a large attack surface and a
version-drift risk to a document whose bytes we checksum. This module emits a valid
PDF 1.4 using only the base-14 fonts, which every conformant reader has built in, and is
byte-for-byte deterministic for a given set of facts.

Scope is deliberately small: Latin-1 text, four base fonts, horizontal rules, and
right/centre alignment. Anything richer belongs in a template service, behind
:class:`app.ports.noc_renderer.NocRenderer`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

# Widths in 1/1000 em for characters 32..126 of the base-14 Helvetica faces.
_HELVETICA = (
    "278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278 "
    "556 556 556 556 556 556 556 556 556 556 278 278 584 584 584 556 1015 "
    "667 667 722 722 667 611 778 722 278 500 667 556 833 722 778 667 778 722 "
    "667 611 722 667 944 667 667 611 278 278 278 469 556 333 "
    "556 556 500 556 556 278 556 556 222 222 500 222 833 556 556 556 556 333 "
    "500 278 556 500 722 500 500 500 334 260 334 584"
)
_HELVETICA_BOLD = (
    "278 333 474 556 556 889 722 238 333 333 389 584 278 333 278 278 "
    "556 556 556 556 556 556 556 556 556 556 333 333 584 584 584 611 975 "
    "722 722 722 722 667 611 778 722 278 556 722 611 833 722 778 667 778 722 "
    "667 611 722 667 944 667 667 611 333 278 333 584 556 333 "
    "556 611 556 611 556 333 611 611 278 278 556 278 889 611 611 611 611 389 "
    "556 333 611 556 778 556 556 500 389 280 389 584"
)


def _width_table(spec: str) -> tuple[int, ...]:
    values = tuple(int(v) for v in spec.split())
    if len(values) != 95:
        raise AssertionError(f"font width table must cover chars 32..126, got {len(values)}")
    return values


FontName = Literal["regular", "bold", "mono", "mono_bold"]

_WIDTHS: dict[FontName, tuple[int, ...] | None] = {
    "regular": _width_table(_HELVETICA),
    "bold": _width_table(_HELVETICA_BOLD),
    "mono": None,  # Courier is monospaced at 600/1000
    "mono_bold": None,
}

_FONT_RESOURCE: dict[FontName, str] = {
    "regular": "F1",
    "bold": "F2",
    "mono": "F3",
    "mono_bold": "F4",
}

_BASE_FONTS = (
    ("F1", "Helvetica"),
    ("F2", "Helvetica-Bold"),
    ("F3", "Courier"),
    ("F4", "Courier-Bold"),
)

A4_WIDTH = 595.28
A4_HEIGHT = 841.89


def sanitise(text: str) -> str:
    """Map arbitrary text onto the WinAnsi range the base-14 fonts can render."""
    return text.encode("cp1252", "replace").decode("cp1252")


def _escape(text: str) -> str:
    out = sanitise(text)
    for old, new in (("\\", r"\\"), ("(", r"\("), (")", r"\)")):
        out = out.replace(old, new)
    return out


def text_width(text: str, font: FontName, size: float) -> float:
    """Width of ``text`` in points."""
    table = _WIDTHS[font]
    if table is None:
        return len(sanitise(text)) * 0.6 * size
    total = 0
    for ch in sanitise(text):
        code = ord(ch)
        total += table[code - 32] if 32 <= code <= 126 else 556
    return total * size / 1000.0


def wrap(text: str, font: FontName, size: float, max_width: float) -> list[str]:
    """Greedy word wrap. Over-long single words are hard-split."""
    lines: list[str] = []
    for paragraph in sanitise(text).split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if text_width(candidate, font, size) <= max_width or not current:
                current = candidate
                while text_width(current, font, size) > max_width and len(current) > 1:
                    cut = len(current) - 1
                    while cut > 1 and text_width(current[:cut], font, size) > max_width:
                        cut -= 1
                    lines.append(current[:cut])
                    current = current[cut:]
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
    return lines


@dataclass
class _Page:
    ops: list[str] = field(default_factory=list)


class PdfDocument:
    """A minimal, deterministic multi-page PDF builder."""

    def __init__(
        self,
        *,
        width: float = A4_WIDTH,
        height: float = A4_HEIGHT,
        margin: float = 56.0,
        title: str = "",
        author: str = "",
        created_at: datetime | None = None,
    ) -> None:
        self.width = width
        self.height = height
        self.margin = margin
        self.title = title
        self.author = author
        self.created_at = created_at
        self._pages: list[_Page] = [_Page()]
        self.y = height - margin

    # ------------------------------------------------------------ geometry
    @property
    def content_width(self) -> float:
        return self.width - 2 * self.margin

    @property
    def _page(self) -> _Page:
        return self._pages[-1]

    def new_page(self) -> None:
        self._pages.append(_Page())
        self.y = self.height - self.margin

    def ensure_space(self, needed: float) -> None:
        if self.y - needed < self.margin:
            self.new_page()

    def space(self, amount: float) -> None:
        self.y -= amount

    # -------------------------------------------------------------- drawing
    def draw_text(
        self,
        text: str,
        *,
        x: float | None = None,
        font: FontName = "regular",
        size: float = 10.0,
        align: Literal["left", "center", "right"] = "left",
        gray: float = 0.0,
        right_edge: float | None = None,
    ) -> None:
        """Draw a single line at the current cursor without advancing it."""
        content = _escape(text)
        if align == "center":
            start = (self.width - text_width(text, font, size)) / 2
        elif align == "right":
            edge = right_edge if right_edge is not None else self.width - self.margin
            start = edge - text_width(text, font, size)
        else:
            start = x if x is not None else self.margin
        ops = self._page.ops
        ops.append("BT")
        if gray:
            ops.append(f"{gray:.3f} g")
        ops.append(f"/{_FONT_RESOURCE[font]} {size:g} Tf")
        ops.append(f"1 0 0 1 {start:.2f} {self.y:.2f} Tm")
        ops.append(f"({content}) Tj")
        ops.append("ET")
        if gray:
            ops.append("0 g")

    def line(
        self,
        text: str,
        *,
        font: FontName = "regular",
        size: float = 10.0,
        leading: float | None = None,
        align: Literal["left", "center", "right"] = "left",
        gray: float = 0.0,
        indent: float = 0.0,
    ) -> None:
        """Draw a line and advance the cursor, paginating if needed."""
        step = leading if leading is not None else size * 1.45
        self.ensure_space(step)
        self.y -= size
        self.draw_text(
            text, x=self.margin + indent, font=font, size=size, align=align, gray=gray
        )
        self.y -= step - size

    def paragraph(
        self,
        text: str,
        *,
        font: FontName = "regular",
        size: float = 10.0,
        leading: float | None = None,
        gray: float = 0.0,
        indent: float = 0.0,
    ) -> None:
        for row in wrap(text, font, size, self.content_width - indent):
            self.line(row, font=font, size=size, leading=leading, gray=gray, indent=indent)

    def rule(self, *, gray: float = 0.75, thickness: float = 0.7, gap: float = 8.0) -> None:
        self.ensure_space(gap * 2)
        self.y -= gap
        self._page.ops.append(
            f"{gray:.3f} G {thickness:g} w {self.margin:.2f} {self.y:.2f} m "
            f"{self.width - self.margin:.2f} {self.y:.2f} l S 0 G"
        )
        self.y -= gap

    def key_value(
        self,
        label: str,
        value: str,
        *,
        size: float = 10.0,
        label_width: float = 150.0,
        leading: float = 15.0,
    ) -> None:
        self.ensure_space(leading)
        self.y -= size
        self.draw_text(label, x=self.margin, font="bold", size=size)
        wrapped = wrap(value, "regular", size, self.content_width - label_width)
        self.draw_text(wrapped[0] if wrapped else "", x=self.margin + label_width, size=size)
        self.y -= leading - size
        for extra in wrapped[1:]:
            self.ensure_space(leading)
            self.y -= size
            self.draw_text(extra, x=self.margin + label_width, size=size)
            self.y -= leading - size

    def money_row(
        self,
        label: str,
        amount: str,
        *,
        bold: bool = False,
        size: float = 10.0,
        leading: float = 15.0,
        indent: float = 0.0,
    ) -> None:
        """A label on the left and a right-aligned monospaced amount on the right."""
        self.ensure_space(leading)
        self.y -= size
        self.draw_text(
            label,
            x=self.margin + indent,
            font="bold" if bold else "regular",
            size=size,
        )
        self.draw_text(
            amount,
            font="mono_bold" if bold else "mono",
            size=size,
            align="right",
        )
        self.y -= leading - size

    # -------------------------------------------------------------- output
    def build(self) -> bytes:
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)  # 1-based object number

        # Reserve: 1=Catalog, 2=Pages; fonts and pages follow.
        catalog_num, pages_num = 1, 2
        objects.extend([b"", b""])

        font_nums: dict[str, int] = {}
        for resource, base_font in _BASE_FONTS:
            font_nums[resource] = add(
                b"<< /Type /Font /Subtype /Type1 /BaseFont /"
                + base_font.encode("ascii")
                + b" /Encoding /WinAnsiEncoding >>"
            )

        font_dict = " ".join(f"/{res} {num} 0 R" for res, num in font_nums.items())

        page_nums: list[int] = []
        for page in self._pages:
            stream = "\n".join(page.ops).encode("cp1252", "replace")
            content_num = add(
                b"<< /Length "
                + str(len(stream)).encode("ascii")
                + b" >>\nstream\n"
                + stream
                + b"\nendstream"
            )
            page_num = add(
                (
                    f"<< /Type /Page /Parent {pages_num} 0 R "
                    f"/MediaBox [0 0 {self.width:.2f} {self.height:.2f}] "
                    f"/Resources << /Font << {font_dict} >> >> "
                    f"/Contents {content_num} 0 R >>"
                ).encode("ascii")
            )
            page_nums.append(page_num)

        kids = " ".join(f"{n} 0 R" for n in page_nums)
        objects[pages_num - 1] = (
            f"<< /Type /Pages /Kids [{kids}] /Count {len(page_nums)} >>".encode("ascii")
        )
        objects[catalog_num - 1] = (
            f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode("ascii")
        )

        info_num = add(self._info_object())

        # ---- serialise with an xref table
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0] * (len(objects) + 1)
        for index, body in enumerate(objects, start=1):
            offsets[index] = len(out)
            out += f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

        xref_offset = len(out)
        count = len(objects) + 1
        out += f"xref\n0 {count}\n".encode("ascii")
        out += b"0000000000 65535 f \n"
        for index in range(1, count):
            out += f"{offsets[index]:010d} 00000 n \n".encode("ascii")

        file_id = hashlib.sha256(bytes(out)).hexdigest()[:32].upper()
        out += (
            f"trailer\n<< /Size {count} /Root {catalog_num} 0 R /Info {info_num} 0 R "
            f"/ID [<{file_id}> <{file_id}>] >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
        return bytes(out)

    def _info_object(self) -> bytes:
        parts = [
            f"/Title ({_escape(self.title)})",
            f"/Author ({_escape(self.author)})",
            "/Producer (meridian-exit-workflow)",
        ]
        if self.created_at is not None:
            stamp = self.created_at.strftime("%Y%m%d%H%M%S")
            parts.append(f"/CreationDate (D:{stamp}+00'00')")
        return ("<< " + " ".join(parts) + " >>").encode("cp1252", "replace")

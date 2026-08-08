"""A minimal, dependency-free PDF 1.4 writer.

Only what the Exit NOC needs: A4 pages, the two standard Type1 Helvetica faces (which every
conforming reader has built in, so nothing needs embedding), left-aligned and centred text,
horizontal rules, and automatic pagination. Output is a byte-exact, deterministic document —
the same inputs always produce the same bytes, which is what makes the stored SHA-256 a
meaningful tamper-evidence check.

Text is encoded as WinAnsi (cp1252); characters outside it are transliterated rather than
silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# --- Adobe standard font metrics (units per 1000 em) for ASCII 32..126 -------------------
# fmt: off
_HELVETICA = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
)
_HELVETICA_BOLD = (
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
    975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
    333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
    611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584,
)
# fmt: on

_FALLBACK_WIDTH = 556

#: Characters outside WinAnsi that appear in real-world address/name data.
_TRANSLITERATE = str.maketrans(
    {
        "‘": "'", "’": "'", "“": '"', "”": '"',
        "–": "-", "—": "-", "…": "...", " ": " ",
        "‏": "", "‎": "",
    }
)

# --- Page geometry (A4, in PostScript points) -------------------------------------------
PAGE_WIDTH = 595.28
PAGE_HEIGHT = 841.89
MARGIN_X = 56.0
MARGIN_TOP = 56.0
MARGIN_BOTTOM = 64.0
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN_X

FONT_REGULAR = "F1"
FONT_BOLD = "F2"


def _widths(bold: bool) -> tuple[int, ...]:
    return _HELVETICA_BOLD if bold else _HELVETICA


def text_width(text: str, size: float, *, bold: bool = False) -> float:
    """Width of ``text`` in points at ``size``, using real Helvetica metrics."""
    table = _widths(bold)
    total = 0
    for ch in text:
        code = ord(ch)
        total += table[code - 32] if 32 <= code <= 126 else _FALLBACK_WIDTH
    return total * size / 1000.0


def wrap_text(text: str, size: float, max_width: float, *, bold: bool = False) -> list[str]:
    """Greedy word wrap. Words longer than ``max_width`` are split character-wise."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}" if current else word
            if text_width(candidate, size, bold=bold) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
            # A single word too wide for the column: hard-split it.
            while text_width(word, size, bold=bold) > max_width:
                cut = len(word)
                while cut > 1 and text_width(word[:cut], size, bold=bold) > max_width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            current = word
        if current:
            lines.append(current)
    return lines or [""]


def _escape(text: str) -> bytes:
    """Escape and encode a string for a PDF literal string object."""
    normalised = text.translate(_TRANSLITERATE)
    out = normalised.encode("cp1252", errors="replace")
    return out.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


# --- Content blocks ----------------------------------------------------------------------


@dataclass(slots=True)
class Heading:
    text: str
    size: float = 15.0
    space_before: float = 14.0
    space_after: float = 6.0
    centered: bool = False


@dataclass(slots=True)
class Paragraph:
    text: str
    size: float = 9.5
    bold: bool = False
    space_before: float = 0.0
    space_after: float = 4.0
    leading: float = 1.45
    centered: bool = False


@dataclass(slots=True)
class KeyValue:
    label: str
    value: str
    size: float = 9.5
    label_width: float = 165.0
    space_after: float = 3.0
    bold_value: bool = False


@dataclass(slots=True)
class Rule:
    space_before: float = 6.0
    space_after: float = 8.0
    thickness: float = 0.6
    gray: float = 0.65


@dataclass(slots=True)
class Spacer:
    height: float = 10.0


Block = Heading | Paragraph | KeyValue | Rule | Spacer


@dataclass(slots=True)
class _Page:
    ops: list[bytes] = field(default_factory=list)


class _Canvas:
    """Accumulates drawing operators, breaking to a new page when the cursor runs out."""

    def __init__(self) -> None:
        self.pages: list[_Page] = [_Page()]
        self.y = PAGE_HEIGHT - MARGIN_TOP

    @property
    def _page(self) -> _Page:
        return self.pages[-1]

    def ensure(self, needed: float) -> None:
        if self.y - needed < MARGIN_BOTTOM:
            self.pages.append(_Page())
            self.y = PAGE_HEIGHT - MARGIN_TOP

    def draw_line(self, text: str, size: float, *, bold: bool, centered: bool = False) -> None:
        self.ensure(size)
        x = MARGIN_X
        if centered:
            x = MARGIN_X + (CONTENT_WIDTH - text_width(text, size, bold=bold)) / 2
        font = FONT_BOLD if bold else FONT_REGULAR
        self._page.ops.append(
            b"BT /%s %s Tf 1 0 0 1 %s %s Tm (%s) Tj ET"
            % (
                font.encode(),
                _num(size),
                _num(x),
                _num(self.y - size),
                _escape(text),
            )
        )
        self.y -= size

    def draw_at(self, x: float, text: str, size: float, *, bold: bool) -> None:
        font = FONT_BOLD if bold else FONT_REGULAR
        self._page.ops.append(
            b"BT /%s %s Tf 1 0 0 1 %s %s Tm (%s) Tj ET"
            % (font.encode(), _num(size), _num(x), _num(self.y - size), _escape(text))
        )

    def draw_rule(self, thickness: float, gray: float) -> None:
        self.ensure(thickness + 1)
        y = self.y
        self._page.ops.append(
            b"q %s G %s w %s %s m %s %s l S Q"
            % (
                _num(gray),
                _num(thickness),
                _num(MARGIN_X),
                _num(y),
                _num(PAGE_WIDTH - MARGIN_X),
                _num(y),
            )
        )
        self.y -= thickness

    def advance(self, amount: float) -> None:
        self.ensure(amount)
        self.y -= amount


def _num(value: float) -> bytes:
    """Format a number compactly and deterministically (no locale, no float noise)."""
    return f"{value:.2f}".rstrip("0").rstrip(".").encode() or b"0"


def _layout(blocks: list[Block]) -> list[_Page]:
    canvas = _Canvas()
    for block in blocks:
        match block:
            case Spacer(height=h):
                canvas.advance(h)
            case Rule(space_before=sb, space_after=sa, thickness=t, gray=g):
                canvas.advance(sb)
                canvas.draw_rule(t, g)
                canvas.advance(sa)
            case Heading(text=text, size=size, space_before=sb, space_after=sa, centered=c):
                canvas.advance(sb)
                for line in wrap_text(text, size, CONTENT_WIDTH, bold=True):
                    canvas.draw_line(line, size, bold=True, centered=c)
                    canvas.advance(size * 0.35)
                canvas.advance(sa)
            case Paragraph(
                text=text, size=size, bold=bold, space_before=sb, space_after=sa,
                leading=leading, centered=c,
            ):
                canvas.advance(sb)
                for line in wrap_text(text, size, CONTENT_WIDTH, bold=bold):
                    canvas.draw_line(line, size, bold=bold, centered=c)
                    canvas.advance(size * (leading - 1.0))
                canvas.advance(sa)
            case KeyValue(
                label=label, value=value, size=size, label_width=lw,
                space_after=sa, bold_value=bv,
            ):
                value_width = CONTENT_WIDTH - lw
                value_lines = wrap_text(value, size, value_width, bold=bv)
                canvas.ensure(size * len(value_lines))
                canvas.draw_at(MARGIN_X, label, size, bold=False)
                for index, line in enumerate(value_lines):
                    canvas.draw_at(MARGIN_X + lw, line, size, bold=bv)
                    canvas.y -= size * (1.0 if index < len(value_lines) - 1 else 0.0)
                canvas.y -= size
                canvas.advance(sa)
    return canvas.pages


def _pdf_date(moment: datetime) -> bytes:
    stamp = moment.strftime("%Y%m%d%H%M%S")
    offset = moment.utcoffset()
    if offset is None:
        return f"D:{stamp}Z".encode()
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return f"D:{stamp}{sign}{total_minutes // 60:02d}'{total_minutes % 60:02d}'".encode()


def render_pdf(
    blocks: list[Block],
    *,
    title: str,
    author: str = "Meridian",
    subject: str = "Exit No Objection Certificate",
    created_at: datetime,
) -> bytes:
    """Render ``blocks`` into a complete PDF document."""
    pages = _layout(blocks)

    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>",
        4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
        b"/Encoding /WinAnsiEncoding >>",
        5: b"<< /Title (%s) /Author (%s) /Subject (%s) /Producer (Meridian Exit Workflow) "
        b"/Creator (Meridian Exit Workflow) /CreationDate (%s) /ModDate (%s) >>"
        % (
            _escape(title),
            _escape(author),
            _escape(subject),
            _pdf_date(created_at),
            _pdf_date(created_at),
        ),
    }

    first_page_obj = 6
    kids: list[bytes] = []
    for index, page in enumerate(pages):
        page_num = first_page_obj + index * 2
        content_num = page_num + 1
        kids.append(b"%d 0 R" % page_num)
        objects[page_num] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %s %s] "
            b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents %d 0 R >>"
            % (_num(PAGE_WIDTH), _num(PAGE_HEIGHT), content_num)
        )
        stream = b"\n".join(page.ops)
        objects[content_num] = b"<< /Length %d >>\nstream\n%s\nendstream" % (
            len(stream),
            stream,
        )

    objects[2] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(kids),
        len(pages),
    )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + objects[num] + b"\nendobj\n"

    xref_offset = len(out)
    count = max(objects) + 1
    out += b"xref\n0 %d\n" % count
    out += b"0000000000 65535 f \n"
    for num in range(1, count):
        out += b"%010d 00000 n \n" % offsets[num]
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 5 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        count,
        xref_offset,
    )
    return bytes(out)

"""PDF rendering of grids with reportlab: landscape A4, one page per grid."""
from __future__ import annotations

import io
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .grid import Grid

FILL = {"lesson": colors.white, "booking": colors.HexColor("#fff3cd"), "rest": colors.HexColor("#eeeeee"), "free": colors.white}
_H = ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=14, leading=17)
_S = ParagraphStyle("s", fontName="Helvetica", fontSize=9, leading=11, textColor=colors.HexColor("#555555"))
_F = ParagraphStyle("f", fontName="Helvetica", fontSize=6.5, leading=8, textColor=colors.HexColor("#555555"))


def _scale(n: int, width: float) -> tuple[float, float, float, float]:
    """Column width and font sizes for a cycle of `n` slots, so one grid stays on one landscape A4
    page. A 26-slot cycle leaves under 30 pt a column; at 7.5 pt every cell would wrap into a column
    of text and the table would run onto a second page."""
    day_w = 44.0 if n > 15 else 60.0
    col = (width - day_w) / max(1, n)
    if col >= 60:
        t_size, u_size = 7.5, 6.5
    elif col >= 40:
        t_size, u_size = 6.5, 5.5
    else:
        t_size, u_size = 5.5, 4.5
    return day_w, col, t_size, u_size


def _clip(s: str, col: float) -> str:
    """Keep a cell to about three lines: roughly col/3 characters fit on one line at these sizes."""
    limit = max(4, int(col / 3))
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "\u2026"


def _styles(t_size: float, u_size: float) -> tuple[ParagraphStyle, ParagraphStyle]:
    return (ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=t_size, leading=t_size + 1.5),
            ParagraphStyle("u", fontName="Helvetica", fontSize=u_size, leading=u_size + 1.5,
                           textColor=colors.HexColor("#555555")))


def _cell(c, col: float, t_st: ParagraphStyle, u_st: ParagraphStyle) -> list:
    if c.kind == "rest":
        return [Paragraph("rest", u_st)]
    if c.kind in ("lesson", "booking"):
        # The lesson name wraps (two lines at worst); only `sub` is clipped, so a cell stays ~3 lines.
        return [Paragraph(escape(c.text), t_st), Paragraph(escape(_clip(c.sub, col * max(1, c.span))), u_st)]
    return ""


def _table(g: Grid, width: float) -> Table:
    n = len(g.slots)
    day_w, col, t_size, u_size = _scale(n, width)
    t_st, u_st = _styles(t_size, u_size)
    pad = 3 if col >= 40 else 2
    data = [[""] + [Paragraph(escape(s), t_st) for s in g.slots]]
    style = [("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#bbbbbb")), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f2f2")),
             ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), pad), ("RIGHTPADDING", (0, 0), (-1, -1), pad)]
    for r, d in enumerate(g.days, start=1):
        row = [Paragraph(escape(d.label), t_st)]
        i = 0
        while i < len(d.cells):
            c = d.cells[i]
            if c.kind == "continued":
                row.append("")
                i += 1
                continue
            row.append(_cell(c, col, t_st, u_st))
            span = max(1, min(c.span, len(d.cells) - i))
            if span > 1:
                style.append(("SPAN", (i + 1, r), (i + span, r)))
                row.extend([""] * (span - 1))
            fill = colors.HexColor("#f7f7f7") if d.off else FILL.get(c.kind, colors.white)
            style.append(("BACKGROUND", (i + 1, r), (i + span, r), fill))
            i += span
        while len(row) < n + 1:
            row.append("")
        data.append(row[:n + 1])
    t = Table(data, colWidths=[day_w] + [col] * n, repeatRows=1)
    t.setStyle(TableStyle(style))
    return t


def render(grids: list[Grid], generated: str, timetable: str) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24,
                            title=f"{timetable} timetables".strip())
    width = landscape(A4)[0] - 48
    story = []
    for i, g in enumerate(grids):
        if i:
            story.append(PageBreak())
        story.append(Paragraph(escape(g.title), _H))
        story.append(Paragraph(escape(" · ".join(x for x in (g.subtitle, timetable) if x)), _S))
        story.append(Spacer(1, 6))
        story.append(_table(g, width))
        story.append(Spacer(1, 6))
        story.append(Paragraph(escape(f"Generated {generated}") if generated else "", _F))

    def footer(canvas, doc_):
        canvas.saveState(); canvas.setFont("Helvetica", 7)
        canvas.drawRightString(landscape(A4)[0] - 24, 12, f"page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()

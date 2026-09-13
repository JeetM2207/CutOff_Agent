"""Compiles LLM-tailored resume sections (cutoff.llm.resume_generate) into a
real PDF via reportlab — the same plain-canvas approach already used by
scripts/make_pdfs.py, eval/runner.py, and server.py's demo shortlist PDF, so
this doesn't add a new PDF-generation dependency to the project."""
from __future__ import annotations

import io

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

_MARGIN = 50
_FONT_BODY = "Helvetica"
_FONT_BODY_SIZE = 10


def _wrap(text: str, *, font: str, size: float, max_width: float) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if stringWidth(candidate, font, size) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


class _Writer:
    """Tracks a y-cursor across drawString calls, paginating when it runs off
    the bottom of the page — reportlab itself has no concept of text flow."""

    def __init__(self, c: canvas.Canvas, width: float, height: float):
        self.c = c
        self.width = width
        self.height = height
        self.y = height - _MARGIN

    def _ensure_room(self, needed: float) -> None:
        if self.y - needed < _MARGIN:
            self.c.showPage()
            self.y = self.height - _MARGIN

    def line(self, text: str, *, font: str = _FONT_BODY, size: float = _FONT_BODY_SIZE,
              leading: float = 13, indent: float = 0) -> None:
        max_width = self.width - 2 * _MARGIN - indent
        self.c.setFont(font, size)
        for wrapped in _wrap(text, font=font, size=size, max_width=max_width):
            self._ensure_room(leading)
            self.c.drawString(_MARGIN + indent, self.y, wrapped)
            self.y -= leading

    def gap(self, amount: float = 8) -> None:
        self.y -= amount


def render_resume_pdf(
    sections: dict, *, student_name: str, student_roll_no: str, student_email: str,
) -> bytes:
    """`sections` is generate_tailored_resume's returned dict: headline,
    skills, highlighted_projects (list of {title, bullets}), match_reason.
    Never raises on merely-empty optional fields — a thin master profile
    should still produce *a* PDF, just a thin one; resume.py's caller treats
    any actual exception here as a reason to fall back to static matching."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    w = _Writer(c, width, height)

    c.setFont("Helvetica-Bold", 16)
    w._ensure_room(20)
    c.drawString(_MARGIN, w.y, student_name)
    w.y -= 20
    w.line(f"{student_roll_no}  |  {student_email}", size=10, leading=16)

    headline = (sections.get("headline") or "").strip()
    if headline:
        w.line(headline, font="Helvetica-Oblique", size=11, leading=15)
        w.gap()

    skills = [s for s in (sections.get("skills") or []) if s]
    if skills:
        w.line("Skills", font="Helvetica-Bold", size=12, leading=16)
        w.line(", ".join(skills), leading=13)
        w.gap()

    for project in sections.get("highlighted_projects") or []:
        title = (project.get("title") or "").strip()
        bullets = [b for b in (project.get("bullets") or []) if b]
        if not title and not bullets:
            continue
        w.line(title or "Project", font="Helvetica-Bold", size=12, leading=16)
        for bullet in bullets:
            w.line(f"-  {bullet}", leading=13, indent=10)
        w.gap()

    c.showPage()
    c.save()
    return buf.getvalue()

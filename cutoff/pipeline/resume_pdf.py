"""Compiles a tailored resume into a real PDF via reportlab — the same
plain-canvas approach already used by scripts/make_pdfs.py, eval/runner.py,
and server.py's demo shortlist PDF (no new PDF dependency).

The section order and layout are FIXED — every generated resume has the same
shape (Header/Contact -> Education -> Headline -> Skills -> Projects ->
Experience -> Achievements). Only headline/skills-subset/project-subset vary
per job description (cutoff.llm.resume_generate's output); Education,
Experience, and Achievements are rendered straight from the master profile,
untouched by the LLM."""
from __future__ import annotations

import io

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from cutoff.models import MasterProfile, StudentProfile

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

    def heading(self, text: str) -> None:
        self._ensure_room(20)
        self.c.setFont("Helvetica-Bold", 12)
        self.c.drawString(_MARGIN, self.y, text)
        self.y -= 6
        self.c.line(_MARGIN, self.y, self.width - _MARGIN, self.y)
        self.y -= 12

    def gap(self, amount: float = 8) -> None:
        self.y -= amount


def _contact_line(student: StudentProfile, master: MasterProfile) -> str:
    parts = [student.roll_no, student.email]
    if master.phone:
        parts.append(master.phone)
    for link in master.links:
        parts.append(f"{link.label}: {link.url}")
    return "  |  ".join(parts)


def render_resume_pdf(sections: dict, *, student_profile: StudentProfile, master_profile: MasterProfile) -> bytes:
    """`sections` is generate_tailored_resume's returned dict: headline,
    skills, highlighted_projects (list of {title, bullets}), match_reason —
    already validated (skills/titles are real master-profile entries) by the
    caller. Never raises on merely-empty optional fields; resume.py's caller
    treats any actual exception here as a reason to fall back to static
    matching."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    w = _Writer(c, width, height)

    # --- Header / contact (deterministic, never LLM-touched) ----------------
    c.setFont("Helvetica-Bold", 16)
    w._ensure_room(20)
    c.drawString(_MARGIN, w.y, student_profile.name)
    w.y -= 20
    w.line(_contact_line(student_profile, master_profile), size=9, leading=13)
    w.gap()

    # --- Education (deterministic) -------------------------------------------
    if master_profile.education:
        w.heading("Education")
        for edu in master_profile.education:
            bits = [edu.degree, edu.institution]
            if edu.cgpa:
                bits.append(f"CGPA {edu.cgpa}")
            if edu.batch_year:
                bits.append(f"Batch {edu.batch_year}")
            w.line(" | ".join(bits), leading=13)
            if edu.notes:
                w.line(edu.notes, size=9, leading=12, indent=8)
        w.gap()

    # --- Headline (LLM-tailored) ----------------------------------------------
    headline = (sections.get("headline") or "").strip()
    if headline:
        w.line(headline, font="Helvetica-Oblique", size=11, leading=15)
        w.gap()

    # --- Skills (LLM-selected subset of master_profile.skills) ---------------
    skills = [s for s in (sections.get("skills") or []) if s]
    if skills:
        w.heading("Skills")
        w.line(", ".join(skills), leading=13)
        w.gap()

    # --- Projects (LLM-selected subset; tech_stack/link looked up from the
    # master profile by title, never from the LLM output) --------------------
    projects_by_title = {p.title.lower(): p for p in master_profile.projects}
    highlighted = sections.get("highlighted_projects") or []
    if highlighted:
        w.heading("Projects")
        for project in highlighted:
            title = (project.get("title") or "").strip()
            bullets = [b for b in (project.get("bullets") or []) if b]
            if not title:
                continue
            original = projects_by_title.get(title.lower())
            header = title
            if original and original.tech_stack:
                header += f"  [{', '.join(original.tech_stack)}]"
            w.line(header, font="Helvetica-Bold", size=11, leading=15)
            for bullet in bullets:
                w.line(f"-  {bullet}", leading=13, indent=10)
            if original and original.link:
                w.line(f"Link: {original.link}", size=9, leading=12, indent=10)
            w.gap(4)
        w.gap(4)

    # --- Experience (deterministic, always shown in full) --------------------
    if master_profile.experience:
        w.heading("Experience")
        for exp in master_profile.experience:
            w.line(exp.title, font="Helvetica-Bold", size=11, leading=15)
            for bullet in exp.bullets:
                w.line(f"-  {bullet}", leading=13, indent=10)
            w.gap(4)
        w.gap(4)

    # --- Achievements (deterministic, always shown in full) -------------------
    if master_profile.achievements:
        w.heading("Achievements")
        for achievement in master_profile.achievements:
            w.line(f"-  {achievement}", leading=13, indent=10)

    c.showPage()
    c.save()
    return buf.getvalue()

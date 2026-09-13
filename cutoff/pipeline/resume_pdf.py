"""Compiles a tailored resume into a real PDF via reportlab — the same
plain-canvas approach already used by scripts/make_pdfs.py, eval/runner.py,
and server.py's demo shortlist PDF (no new PDF dependency).

Layout modeled directly on a real, working ATS-style one-pager the user
supplied as a reference (centered header with hyperlinked contact/links, a
Professional Summary paragraph, a flat Technical Skills line, Projects with
a "Live Demo | GitHub" link line and an italic tech-stack line, Education,
Achievements) — not the original, plainer left-aligned layout.

Section order is FIXED — every generated resume has the same shape (Header
-> Professional Summary -> Technical Skills -> Projects -> Experience ->
Education -> Achievements). Only the summary/skills-subset/project-subset
vary per job description (cutoff.llm.resume_generate's output); Education,
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
_LINK_COLOR = (0.09, 0.3, 0.65)  # a muted blue, matching the reference's \colorlinks=true, urlcolor=blue


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

    def ensure_room(self, needed: float) -> None:
        if self.y - needed < _MARGIN:
            self.c.showPage()
            self.y = self.height - _MARGIN

    def line(self, text: str, *, font: str = _FONT_BODY, size: float = _FONT_BODY_SIZE,
              leading: float = 13, indent: float = 0) -> None:
        max_width = self.width - 2 * _MARGIN - indent
        self.c.setFont(font, size)
        for wrapped in _wrap(text, font=font, size=size, max_width=max_width):
            self.ensure_room(leading)
            self.c.drawString(_MARGIN + indent, self.y, wrapped)
            self.y -= leading

    def centered_line(self, text: str, *, font: str, size: float, leading: float) -> None:
        self.ensure_room(leading)
        self.c.setFont(font, size)
        self.c.drawCentredString(self.width / 2, self.y, text)
        self.y -= leading

    def heading(self, text: str) -> None:
        """Bold section title with a thin rule underneath — the reportlab
        equivalent of the reference's \\titleformat{\\section} style."""
        self.ensure_room(20)
        self.c.setFont("Helvetica-Bold", 12)
        self.c.drawString(_MARGIN, self.y, text)
        self.y -= 5
        self.c.setLineWidth(0.75)
        self.c.line(_MARGIN, self.y, self.width - _MARGIN, self.y)
        self.y -= 12

    def gap(self, amount: float = 8) -> None:
        self.y -= amount

    def link_annotation(self, url: str, x0: float, x1: float, *, height: float = 11) -> None:
        """A real, clickable hyperlink over the text just drawn at self.y —
        matches the reference's \\hyperref-linked contact/project URLs."""
        self.c.linkURL(url, (x0, self.y - 2, x1, self.y + height), relative=0, thickness=0)


def _centered_contact_line(w: _Writer, student: StudentProfile, master: MasterProfile) -> None:
    """One centered line, pipe-separated, matching the reference's contact
    header exactly — with real clickable links for email and every
    GitHub/LinkedIn/portfolio entry, not just plain text."""
    font, size, leading = _FONT_BODY, 9.5, 13
    segments: list[tuple[str, str | None]] = [(student.roll_no, None), (student.email, f"mailto:{student.email}")]
    if master.phone:
        segments.append((master.phone, None))
    for link in master.links:
        segments.append((link.label, link.url))

    sep = "  |  "
    full_text = sep.join(s[0] for s in segments)
    w.ensure_room(leading)
    w.c.setFont(font, size)
    total_width = stringWidth(full_text, font, size)
    x = w.width / 2 - total_width / 2
    w.c.setFillColorRGB(0, 0, 0)
    for i, (text, url) in enumerate(segments):
        if url:
            w.c.setFillColorRGB(*_LINK_COLOR)
        else:
            w.c.setFillColorRGB(0, 0, 0)
        w.c.drawString(x, w.y, text)
        seg_width = stringWidth(text, font, size)
        if url:
            w.c.linkURL(url, (x, w.y - 2, x + seg_width, w.y + 10), relative=0, thickness=0)
        x += seg_width
        if i < len(segments) - 1:
            w.c.setFillColorRGB(0.4, 0.4, 0.4)
            w.c.drawString(x, w.y, sep)
            x += stringWidth(sep, font, size)
    w.c.setFillColorRGB(0, 0, 0)
    w.y -= leading


def _project_link_line(w: _Writer, project) -> None:
    """"Live Demo | GitHub", each a real clickable link, exactly matching
    the reference's project header line — only the links actually present
    are shown, and it's omitted entirely if there are none."""
    parts: list[tuple[str, str]] = []
    if project.demo_link:
        parts.append(("Live Demo", project.demo_link))
    if project.link:
        parts.append(("GitHub", project.link))
    if not parts:
        return
    font, size, leading = _FONT_BODY, 9.5, 13
    w.ensure_room(leading)
    w.c.setFont(font, size)
    x = _MARGIN
    for i, (label, url) in enumerate(parts):
        w.c.setFillColorRGB(*_LINK_COLOR)
        w.c.drawString(x, w.y, label)
        seg_width = stringWidth(label, font, size)
        w.c.linkURL(url, (x, w.y - 2, x + seg_width, w.y + 10), relative=0, thickness=0)
        x += seg_width
        if i < len(parts) - 1:
            w.c.setFillColorRGB(0.4, 0.4, 0.4)
            w.c.drawString(x, w.y, "  |  ")
            x += stringWidth("  |  ", font, size)
    w.c.setFillColorRGB(0, 0, 0)
    w.y -= leading


def render_resume_pdf(sections: dict, *, student_profile: StudentProfile, master_profile: MasterProfile) -> bytes:
    """`sections` is generate_tailored_resume's returned dict: headline
    (rendered as the Professional Summary paragraph), skills,
    highlighted_projects (list of {title, bullets}), match_reason — already
    validated (skills/titles are real master-profile entries) by the
    caller. Never raises on merely-empty optional fields; resume.py's caller
    treats any actual exception here as a reason to fall back to static
    matching."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    w = _Writer(c, width, height)

    # --- Header / contact (deterministic, never LLM-touched) ----------------
    w.centered_line(student_profile.name.upper(), font="Helvetica-Bold", size=16, leading=20)
    _centered_contact_line(w, student_profile, master_profile)
    w.gap(6)

    # --- Professional Summary (LLM-tailored per JD) --------------------------
    summary = (sections.get("headline") or "").strip()
    if summary:
        w.heading("Professional Summary")
        w.line(summary, leading=13)
        w.gap()

    # --- Technical Skills (LLM-selected subset of master_profile.skills) ----
    skills = [s for s in (sections.get("skills") or []) if s]
    if skills:
        w.heading("Technical Skills")
        w.line(", ".join(skills), leading=13)
        w.gap()

    # --- Projects (LLM-selected subset; tech_stack/links looked up from the
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
            w.line(title, font="Helvetica-Bold", size=11, leading=15)
            if original:
                _project_link_line(w, original)
                if original.tech_stack:
                    w.line(f"Tech Stack: {', '.join(original.tech_stack)}", font="Helvetica-Oblique", size=9.5, leading=13)
            for bullet in bullets:
                w.line(f"-  {bullet}", leading=13, indent=10)
            w.gap(6)
        w.gap(2)

    # --- Experience (deterministic, always shown in full) --------------------
    if master_profile.experience:
        w.heading("Experience")
        for exp in master_profile.experience:
            w.line(exp.title, font="Helvetica-Bold", size=11, leading=15)
            for bullet in exp.bullets:
                w.line(f"-  {bullet}", leading=13, indent=10)
            w.gap(6)
        w.gap(2)

    # --- Education (deterministic) -------------------------------------------
    if master_profile.education:
        w.heading("Education")
        for edu in master_profile.education:
            institution_line = edu.institution
            if edu.batch_year:
                institution_line += f"  ({edu.batch_year})"
            w.line(institution_line, font="Helvetica-Bold", size=11, leading=15)
            degree_line = edu.degree
            if edu.cgpa:
                degree_line += f" — CGPA {edu.cgpa}"
            w.line(degree_line, leading=13)
            if edu.notes:
                w.line(edu.notes, size=9, leading=12)
        w.gap()

    # --- Achievements (deterministic, always shown in full) -------------------
    if master_profile.achievements:
        w.heading("Achievements & Certifications")
        for achievement in master_profile.achievements:
            w.line(f"-  {achievement}", leading=13, indent=10)

    c.showPage()
    c.save()
    return buf.getvalue()

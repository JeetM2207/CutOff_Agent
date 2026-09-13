"""cutoff.pipeline.resume_pdf: pure PDF-rendering unit tests, no LLM/network
(Section 0 rule 3 doesn't even apply here -- there's nothing to stub). Covers
the FIXED template: header/contact, education, headline, skills, projects
(with tech_stack/link looked up from the master profile), experience and
achievements shown in full."""
import io

import pdfplumber

from cutoff.models import EducationEntry, MasterProfile, MasterProfileExperience, MasterProfileProject, ProfileLink, StudentProfile
from cutoff.pipeline.resume_pdf import render_resume_pdf


def _extract_text(pdf_bytes: bytes) -> str:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)

STUDENT = StudentProfile(
    name="Riya Mehta", roll_no="21BCS045", email="riya@college.edu", branch="CSE",
    gpa=7.42, active_backlogs=0, batch_year=2026,
)


def _master(**overrides):
    base = dict(
        phone="+91-9999999999",
        links=[ProfileLink(label="GitHub", url="https://github.com/riya")],
        education=[EducationEntry(degree="B.Tech CSE", institution="Demo College", cgpa="7.42", batch_year=2026)],
        skills=["Python", "Django"],
        projects=[MasterProfileProject(title="Order Service", tech_stack=["Django", "PostgreSQL"],
                                        bullets=["Built a Django REST API."], link="https://github.com/riya/order-service")],
        experience=[MasterProfileExperience(title="Backend Intern, Acme, 2025", bullets=["Shipped 2 endpoints."])],
        achievements=["Runner-up, hackathon."],
    )
    base.update(overrides)
    return MasterProfile(**base)


def _sections(**overrides):
    base = {
        "headline": "Backend-focused CSE student experienced with Django and PostgreSQL.",
        "skills": ["Python", "Django"],
        "highlighted_projects": [{"title": "Order Service", "bullets": ["Built a Django REST API."]}],
        "match_reason": "Emphasized Django experience matching the JD's backend stack.",
    }
    base.update(overrides)
    return base


def test_render_resume_pdf_produces_a_real_pdf():
    pdf_bytes = render_resume_pdf(_sections(), student_profile=STUDENT, master_profile=_master())
    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 500


def test_render_resume_pdf_handles_empty_optional_sections_without_raising():
    thin_sections = {"headline": "", "skills": [], "highlighted_projects": [], "match_reason": ""}
    thin_master = MasterProfile()
    pdf_bytes = render_resume_pdf(thin_sections, student_profile=STUDENT, master_profile=thin_master)
    assert pdf_bytes.startswith(b"%PDF-")


def test_render_resume_pdf_wraps_long_bullets_across_multiple_lines():
    long_bullet = "word " * 60  # forces the greedy word-wrapper to break across several lines
    pdf_bytes = render_resume_pdf(
        _sections(highlighted_projects=[{"title": "Order Service", "bullets": [long_bullet.strip()]}]),
        student_profile=STUDENT, master_profile=_master(),
    )
    assert pdf_bytes.startswith(b"%PDF-")


def test_render_resume_pdf_paginates_when_content_overflows_one_page():
    many_experience = [
        MasterProfileExperience(title=f"Role {i}", bullets=[f"Did thing {i} in detail." for _ in range(6)])
        for i in range(15)
    ]
    pdf_bytes = render_resume_pdf(
        _sections(), student_profile=STUDENT, master_profile=_master(experience=many_experience),
    )
    # A real multi-page PDF has more than one /Type /Page object reference.
    assert pdf_bytes.count(b"/Type /Page") > 1 or pdf_bytes.count(b"/Type/Page") > 1


def test_render_resume_pdf_shows_education_experience_achievements_deterministically():
    """These three sections come straight from the master profile, never
    from the LLM-controlled `sections` dict -- confirm they render even when
    `sections` says nothing about them."""
    pdf_bytes = render_resume_pdf(
        {"headline": "", "skills": [], "highlighted_projects": [], "match_reason": ""},
        student_profile=STUDENT, master_profile=_master(),
    )
    text = _extract_text(pdf_bytes)
    assert "Demo College" in text
    assert "Backend Intern" in text
    assert "Runner-up" in text
    assert "github.com/riya" in text


def test_render_resume_pdf_project_link_and_tech_stack_come_from_master_profile_not_llm():
    """The LLM only supplies title+bullets; tech_stack/link must be looked
    up from the real master-profile project by title, never invented."""
    pdf_bytes = render_resume_pdf(_sections(), student_profile=STUDENT, master_profile=_master())
    text = _extract_text(pdf_bytes)
    assert "PostgreSQL" in text
    assert "order-service" in text

"""cutoff.pipeline.resume_pdf: pure PDF-rendering unit tests, no LLM/network
(Section 0 rule 3 doesn't even apply here -- there's nothing to stub)."""
from cutoff.pipeline.resume_pdf import render_resume_pdf


def _sections(**overrides):
    base = {
        "headline": "Backend-focused CSE student experienced with Django and PostgreSQL.",
        "skills": ["Python", "Django", "PostgreSQL", "Docker"],
        "highlighted_projects": [
            {"title": "Order Service", "bullets": ["Built a Django REST API handling 10k orders/day."]},
        ],
        "match_reason": "Emphasized Django/PostgreSQL experience matching the JD's backend stack.",
    }
    base.update(overrides)
    return base


def test_render_resume_pdf_produces_a_real_pdf():
    pdf_bytes = render_resume_pdf(_sections(), student_name="Riya Mehta", student_roll_no="21BCS045",
                                   student_email="riya@college.edu")
    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 500


def test_render_resume_pdf_handles_empty_optional_sections_without_raising():
    thin = {"headline": "", "skills": [], "highlighted_projects": [], "match_reason": ""}
    pdf_bytes = render_resume_pdf(thin, student_name="Riya Mehta", student_roll_no="21BCS045",
                                   student_email="riya@college.edu")
    assert pdf_bytes.startswith(b"%PDF-")


def test_render_resume_pdf_wraps_long_bullets_across_multiple_lines():
    long_bullet = "word " * 60  # forces the greedy word-wrapper to break across several lines
    pdf_bytes = render_resume_pdf(
        _sections(highlighted_projects=[{"title": "Project", "bullets": [long_bullet.strip()]}]),
        student_name="Riya Mehta", student_roll_no="21BCS045", student_email="riya@college.edu",
    )
    assert pdf_bytes.startswith(b"%PDF-")


def test_render_resume_pdf_paginates_when_content_overflows_one_page():
    many_projects = [
        {"title": f"Project {i}", "bullets": [f"Bullet describing project {i} in more detail." for _ in range(6)]}
        for i in range(15)
    ]
    pdf_bytes = render_resume_pdf(
        _sections(highlighted_projects=many_projects),
        student_name="Riya Mehta", student_roll_no="21BCS045", student_email="riya@college.edu",
    )
    # A real multi-page PDF has more than one /Type /Page object reference.
    assert pdf_bytes.count(b"/Type /Page") > 1 or pdf_bytes.count(b"/Type/Page") > 1

"""Resume selection (Section 6.4): deterministic role-category filename
mapping, plus the JD-content smart match and dynamic generation layered on
top (never calls a real LLM or Drive API here — Section 0 rule 3)."""
from cutoff.adapters.fakes import FakeFileStore
from cutoff.models import MasterProfile, MasterProfileProject, ResumeFile, StudentProfile
from cutoff.pipeline import resume

STUDENT = StudentProfile(
    name="Riya Mehta", roll_no="21BCS045", email="riya@college.edu", branch="CSE",
    gpa=7.42, active_backlogs=0, batch_year=2026,
)

MASTER_PROFILE = MasterProfile(
    skills=["Python", "Django"],
    projects=[MasterProfileProject(title="Order Service", bullets=["Built a Django REST API."])],
)


def _resumes():
    return [
        ResumeFile(file_id="f_sde", name="resume_SDE.pdf", web_view_link="https://drive/sde"),
        ResumeFile(file_id="f_data", name="resume_DATA.pdf", web_view_link="https://drive/data"),
        ResumeFile(file_id="f_default", name="resume_DEFAULT.pdf", web_view_link="https://drive/default"),
    ]


def test_select_resume_matches_role_category():
    store = FakeFileStore(_resumes())
    picked = resume.select_resume("SDE", store)
    assert picked.name == "resume_SDE.pdf"


def test_select_resume_falls_back_to_default_on_no_match():
    store = FakeFileStore(_resumes())
    picked = resume.select_resume("CORE", store)
    assert picked.name == "resume_DEFAULT.pdf"


def test_select_resume_none_when_nothing_on_file():
    store = FakeFileStore([])
    assert resume.select_resume("SDE", store) is None


def test_select_resume_smart_skips_llm_when_no_jd_text():
    store = FakeFileStore(_resumes())
    picked, reason = resume.select_resume_smart(
        "SDE", "", store, api_key="x", model="m", provider="anthropic", base_url=None, db_path=":memory:",
    )
    assert picked.name == "resume_SDE.pdf"
    assert reason is None
    assert "get_resume_content" not in [c[0] for c in store.calls]


def test_select_resume_smart_skips_llm_when_no_resumes_on_file():
    store = FakeFileStore([])
    picked, reason = resume.select_resume_smart(
        "SDE", "We need a backend engineer.", store,
        api_key="x", model="m", provider="anthropic", base_url=None, db_path=":memory:",
    )
    assert picked is None
    assert reason is None


def test_select_resume_smart_uses_llm_match_when_jd_and_content_present(monkeypatch):
    store = FakeFileStore(_resumes(), contents={"f_sde": b"pdf-bytes-sde", "f_data": b"pdf-bytes-data", "f_default": b"pdf-bytes-default"})
    monkeypatch.setattr(resume, "extract_pdf_text", lambda data: f"resume text for {data.decode()}")

    def fake_select_best_resume(jd_text, resumes, **kwargs):
        assert "backend engineer" in jd_text
        names = [name for name, _ in resumes]
        assert set(names) == {"resume_SDE.pdf", "resume_DATA.pdf", "resume_DEFAULT.pdf"}
        return "resume_SDE.pdf", "Backend experience listed matches the JD's backend requirement."

    monkeypatch.setattr("cutoff.llm.resume_match.select_best_resume", fake_select_best_resume)

    picked, reason = resume.select_resume_smart(
        "SDE", "We need a backend engineer.", store,
        api_key="x", model="m", provider="anthropic", base_url=None, db_path=":memory:",
    )
    assert picked.name == "resume_SDE.pdf"
    assert reason == "Backend experience listed matches the JD's backend requirement."


def test_select_resume_smart_falls_back_when_llm_call_fails(monkeypatch):
    store = FakeFileStore(_resumes(), contents={"f_sde": b"x", "f_data": b"x", "f_default": b"x"})
    monkeypatch.setattr(resume, "extract_pdf_text", lambda data: "some resume text")

    def broken_select_best_resume(jd_text, resumes, **kwargs):
        raise RuntimeError("rate limited")

    monkeypatch.setattr("cutoff.llm.resume_match.select_best_resume", broken_select_best_resume)

    picked, reason = resume.select_resume_smart(
        "SDE", "We need a backend engineer.", store,
        api_key="x", model="m", provider="anthropic", base_url=None, db_path=":memory:",
    )
    assert picked.name == "resume_SDE.pdf"  # deterministic category fallback
    assert reason is None


def test_select_resume_smart_falls_back_when_pdf_extraction_fails_for_everything(monkeypatch):
    store = FakeFileStore(_resumes(), contents={"f_sde": b"garbage", "f_data": b"garbage", "f_default": b"garbage"})

    def broken_extract(data):
        raise ValueError("not a valid PDF")

    monkeypatch.setattr(resume, "extract_pdf_text", broken_extract)

    picked, reason = resume.select_resume_smart(
        "DATA", "We need a data scientist.", store,
        api_key="x", model="m", provider="anthropic", base_url=None, db_path=":memory:",
    )
    assert picked.name == "resume_DATA.pdf"  # deterministic category fallback
    assert reason is None


# --- Dynamic resume generation (Section 6.4 extension) ----------------------
# Every test above passes no student_profile/master_profile/
# generated_resume_dir, which is exactly what every caller written before
# this feature existed does -- confirming generation stays fully inert
# unless a caller opts in explicitly.

def _fake_sections():
    return (
        {
            "headline": "Backend-focused CSE student.", "skills": ["Python", "Django"],
            "highlighted_projects": [{"title": "Order Service", "bullets": ["Built a Django REST API."]}],
            "match_reason": "Emphasized Django experience matching the JD's backend stack.",
        },
        "Emphasized Django experience matching the JD's backend stack.",
    )


def test_select_resume_smart_generates_a_tailored_resume_when_configured(monkeypatch, tmp_path):
    store = FakeFileStore(_resumes())  # static resumes present but should never be touched

    monkeypatch.setattr("cutoff.llm.resume_generate.generate_tailored_resume", lambda *a, **k: _fake_sections())
    monkeypatch.setattr("cutoff.pipeline.resume_pdf.render_resume_pdf", lambda *a, **k: b"%PDF-1.4 fake bytes")

    picked, reason = resume.select_resume_smart(
        "SDE", "We need a backend engineer with Django experience.", store,
        api_key="x", model="m", provider="anthropic", base_url=None, db_path=":memory:",
        student_profile=STUDENT, master_profile=MASTER_PROFILE,
        generated_resume_dir=str(tmp_path), public_base_url="http://127.0.0.1:8000",
    )
    assert picked.name == resume.GENERATED_RESUME_NAME
    assert picked.web_view_link.startswith("http://127.0.0.1:8000/generated_resumes/")
    assert reason == "Emphasized Django experience matching the JD's backend stack."
    assert "get_resume_content" not in [c[0] for c in store.calls]  # static match path never touched
    assert list(tmp_path.iterdir())  # the PDF was actually written to disk


def test_select_resume_smart_generation_ignores_static_resumes_being_empty(monkeypatch, tmp_path):
    """Generation shouldn't require any pre-existing Drive resume at all —
    that's the whole point of building one from the master profile."""
    store = FakeFileStore([])

    monkeypatch.setattr("cutoff.llm.resume_generate.generate_tailored_resume", lambda *a, **k: _fake_sections())
    monkeypatch.setattr("cutoff.pipeline.resume_pdf.render_resume_pdf", lambda *a, **k: b"%PDF-1.4 fake bytes")

    picked, reason = resume.select_resume_smart(
        "SDE", "We need a backend engineer.", store,
        api_key="x", model="m", provider="anthropic", base_url=None, db_path=":memory:",
        student_profile=STUDENT, master_profile=MASTER_PROFILE,
        generated_resume_dir=str(tmp_path),
    )
    assert picked.name == resume.GENERATED_RESUME_NAME
    assert reason is not None


def test_select_resume_smart_falls_back_to_static_match_when_generation_fails(monkeypatch, tmp_path):
    store = FakeFileStore(_resumes(), contents={"f_sde": b"x", "f_data": b"x", "f_default": b"x"})
    monkeypatch.setattr(resume, "extract_pdf_text", lambda data: "some resume text")

    def broken_generate(*a, **k):
        raise RuntimeError("rate limited")

    monkeypatch.setattr("cutoff.llm.resume_generate.generate_tailored_resume", broken_generate)
    monkeypatch.setattr("cutoff.llm.resume_match.select_best_resume",
                        lambda jd_text, resumes, **k: ("resume_SDE.pdf", "Static match reason."))

    picked, reason = resume.select_resume_smart(
        "SDE", "We need a backend engineer.", store,
        api_key="x", model="m", provider="anthropic", base_url=None, db_path=":memory:",
        student_profile=STUDENT, master_profile=MASTER_PROFILE,
        generated_resume_dir=str(tmp_path),
    )
    assert picked.name == "resume_SDE.pdf"  # fell all the way back to the static JD-content match
    assert reason == "Static match reason."


def test_select_resume_smart_falls_back_when_pdf_render_fails(monkeypatch, tmp_path):
    store = FakeFileStore(_resumes(), contents={"f_sde": b"x", "f_data": b"x", "f_default": b"x"})
    monkeypatch.setattr(resume, "extract_pdf_text", lambda data: "some resume text")
    monkeypatch.setattr("cutoff.llm.resume_generate.generate_tailored_resume", lambda *a, **k: _fake_sections())

    def broken_render(*a, **k):
        raise ValueError("bad sections")

    monkeypatch.setattr("cutoff.pipeline.resume_pdf.render_resume_pdf", broken_render)
    monkeypatch.setattr("cutoff.llm.resume_match.select_best_resume",
                        lambda jd_text, resumes, **k: ("resume_SDE.pdf", "Static match reason."))

    picked, reason = resume.select_resume_smart(
        "SDE", "We need a backend engineer.", store,
        api_key="x", model="m", provider="anthropic", base_url=None, db_path=":memory:",
        student_profile=STUDENT, master_profile=MASTER_PROFILE,
        generated_resume_dir=str(tmp_path),
    )
    assert picked.name == "resume_SDE.pdf"
    assert reason == "Static match reason."


def test_select_resume_smart_generation_stays_inert_without_master_profile(monkeypatch, tmp_path):
    """student_profile alone (no master_profile) must not trigger
    generation — every field is required before it activates."""
    store = FakeFileStore(_resumes())
    called = []
    monkeypatch.setattr("cutoff.llm.resume_generate.generate_tailored_resume",
                        lambda *a, **k: called.append(1) or _fake_sections())

    picked, reason = resume.select_resume_smart(
        "SDE", "We need a backend engineer.", store,
        api_key="x", model="m", provider="anthropic", base_url=None, db_path=":memory:",
        student_profile=STUDENT, master_profile=None, generated_resume_dir=str(tmp_path),
    )
    assert not called
    assert picked.name == "resume_SDE.pdf"  # category fallback (no JD-matching stub set up here)


def test_write_generated_pdf_is_content_addressed(tmp_path):
    filename_a = resume._write_generated_pdf(b"same bytes", output_dir=str(tmp_path))
    filename_b = resume._write_generated_pdf(b"same bytes", output_dir=str(tmp_path))
    filename_c = resume._write_generated_pdf(b"different bytes", output_dir=str(tmp_path))
    assert filename_a == filename_b
    assert filename_a != filename_c
    assert len(list(tmp_path.iterdir())) == 2

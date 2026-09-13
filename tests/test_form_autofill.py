"""form_autofill.build_autofilled_url: ties scraped form fields, keyword
matching against StudentProfile, and LLM-drafted open-ended answers
together. Never calls real network/LLM (Section 0 rule 3) — fetch_form_page
and llm.form_answer.draft_answers are monkeypatched."""
from urllib.parse import parse_qs, urlparse

from cutoff.adapters.forms_scrape import FormField
from cutoff.models import StudentProfile
from cutoff.pipeline import form_autofill

PROFILE = StudentProfile(
    name="Riya Mehta", roll_no="21BCS045", email="riya@college.edu", branch="CSE",
    gpa=7.42, active_backlogs=0, batch_year=2026,
)

_KWARGS = dict(api_key="x", model="m", provider="anthropic", base_url=None)


def test_returns_none_when_form_url_empty():
    assert form_autofill.build_autofilled_url("", PROFILE, "resume text", "jd text", **_KWARGS) is None


def test_resume_link_field_filled_deterministically_not_via_llm(monkeypatch):
    fields = [
        FormField(entry_id="111", title="Name", field_type=0, required=True),
        FormField(entry_id="555", title="Submit your cover letter or resume(link)", field_type=0, required=False),
    ]
    monkeypatch.setattr(form_autofill, "fetch_form_page", lambda url: ("https://docs.google.com/forms/d/e/X/viewform", "<html/>"))
    monkeypatch.setattr(form_autofill, "parse_form_fields", lambda html: fields)

    def _should_not_be_called(*a, **k):
        raise AssertionError("a resume-link field must never reach the LLM drafter")

    monkeypatch.setattr("cutoff.llm.form_answer.draft_answers", _should_not_be_called)

    url = form_autofill.build_autofilled_url(
        "https://forms.gle/x", PROFILE, "resume text", "jd text",
        resume_link="https://drive.google.com/file/d/abc123/view", **_KWARGS,
    )
    params = parse_qs(urlparse(url).query)
    assert params["entry.111"] == ["Riya Mehta"]
    assert params["entry.555"] == ["https://drive.google.com/file/d/abc123/view"]


def test_returns_none_when_page_unreachable(monkeypatch):
    monkeypatch.setattr(form_autofill, "fetch_form_page", lambda url: None)
    assert form_autofill.build_autofilled_url("https://forms.gle/x", PROFILE, "resume", "jd", **_KWARGS) is None


def test_fills_known_profile_fields_deterministically_no_llm_call(monkeypatch):
    fields = [
        FormField(entry_id="111", title="Full Name", field_type=0, required=True),
        FormField(entry_id="222", title="Roll Number", field_type=0, required=True),
        FormField(entry_id="333", title="Branch", field_type=0, required=False),
    ]
    monkeypatch.setattr(form_autofill, "fetch_form_page", lambda url: ("https://docs.google.com/forms/d/e/X/viewform", "<html/>"))
    monkeypatch.setattr(form_autofill, "parse_form_fields", lambda html: fields)

    def _should_not_be_called(*a, **k):
        raise AssertionError("draft_answers should not be called when every field matched deterministically")

    monkeypatch.setattr("cutoff.llm.form_answer.draft_answers", _should_not_be_called)

    url = form_autofill.build_autofilled_url("https://forms.gle/x", PROFILE, "resume text", "jd text", **_KWARGS)
    assert url is not None
    params = parse_qs(urlparse(url).query)
    assert params["entry.111"] == ["Riya Mehta"]
    assert params["entry.222"] == ["21BCS045"]
    assert params["entry.333"] == ["CSE"]


def test_drafts_open_ended_question_via_llm_grounded_in_resume(monkeypatch):
    fields = [
        FormField(entry_id="111", title="Full Name", field_type=0, required=True),
        FormField(entry_id="444", title="Why should we hire you?", field_type=1, required=False),
    ]
    monkeypatch.setattr(form_autofill, "fetch_form_page", lambda url: ("https://docs.google.com/forms/d/e/X/viewform", "<html/>"))
    monkeypatch.setattr(form_autofill, "parse_form_fields", lambda html: fields)

    def fake_draft_answers(resume_text, jd_text, titles, **kwargs):
        assert titles == ["Why should we hire you?"]
        assert "backend" in resume_text
        return {"Why should we hire you?": "I've built backend systems in Python, matching this role."}

    monkeypatch.setattr("cutoff.llm.form_answer.draft_answers", fake_draft_answers)

    url = form_autofill.build_autofilled_url(
        "https://forms.gle/x", PROFILE, "Experienced in backend Python development.", "Looking for a backend engineer.",
        **_KWARGS,
    )
    params = parse_qs(urlparse(url).query)
    assert params["entry.111"] == ["Riya Mehta"]
    assert params["entry.444"] == ["I've built backend systems in Python, matching this role."]


def test_leaves_question_blank_when_llm_returns_empty_string(monkeypatch):
    fields = [FormField(entry_id="444", title="What's your salary expectation?", field_type=0, required=False)]
    monkeypatch.setattr(form_autofill, "fetch_form_page", lambda url: ("https://docs.google.com/forms/d/e/X/viewform", "<html/>"))
    monkeypatch.setattr(form_autofill, "parse_form_fields", lambda html: fields)
    monkeypatch.setattr("cutoff.llm.form_answer.draft_answers", lambda *a, **k: {"What's your salary expectation?": ""})

    url = form_autofill.build_autofilled_url("https://forms.gle/x", PROFILE, "resume text", "jd text", **_KWARGS)
    assert url is None  # nothing was actually fillable


def test_never_fills_a_choice_question(monkeypatch):
    fields = [FormField(entry_id="999", title="Years of experience", field_type=2, required=True)]
    monkeypatch.setattr(form_autofill, "fetch_form_page", lambda url: ("https://docs.google.com/forms/d/e/X/viewform", "<html/>"))
    monkeypatch.setattr(form_autofill, "parse_form_fields", lambda html: [])  # filtered out before reaching here

    def _should_not_be_called(*a, **k):
        raise AssertionError("a choice-type question must never reach the LLM drafting step")

    monkeypatch.setattr("cutoff.llm.form_answer.draft_answers", _should_not_be_called)

    assert form_autofill.build_autofilled_url("https://forms.gle/x", PROFILE, "resume", "jd", **_KWARGS) is None


def test_returns_none_and_does_not_raise_when_llm_call_fails(monkeypatch):
    fields = [FormField(entry_id="444", title="Why should we hire you?", field_type=1, required=False)]
    monkeypatch.setattr(form_autofill, "fetch_form_page", lambda url: ("https://docs.google.com/forms/d/e/X/viewform", "<html/>"))
    monkeypatch.setattr(form_autofill, "parse_form_fields", lambda html: fields)

    def _broken(*a, **k):
        raise RuntimeError("rate limited")

    monkeypatch.setattr("cutoff.llm.form_answer.draft_answers", _broken)

    url = form_autofill.build_autofilled_url("https://forms.gle/x", PROFILE, "resume", "jd", **_KWARGS)
    assert url is None

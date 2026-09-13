"""End-to-end: PipelineContext.autofill_form_fn (Section 6.5 extension) is
opt-in — None by default (every existing test, and every fixture, stays on
the old zero-network behavior) — and when set, its result wins over a
stored FormTemplates entry, which in turn wins over the plain form_url.
Never calls a real LLM, Sheets, or network (Section 0 rule 3): the injected
autofill_form_fn is a plain Python stub, not the real implementation."""
from datetime import datetime, timezone

from cutoff import db
from cutoff.adapters.fakes import FakeCalendarStore, FakeFileStore, FakeMailSource, FakeMessenger, FakeSheetStore
from cutoff.models import CollegePolicy, Criteria, EmailMessage, Evidence, Notice, ResumeFile, StudentProfile
from cutoff.pipeline import executor, run
from cutoff.pipeline.executor import Adapters

PROFILE = StudentProfile(
    name="Riya Mehta", roll_no="21BCS045", email="riya@college.edu", branch="CSE",
    gpa=7.42, active_backlogs=0, batch_year=2026,
)
POLICY = CollegePolicy(college_domain="college.edu", career_office_senders=["careers.demo.college@gmail.com"])
NOW = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)


def _stub_extract(msg, body_clean, quoted_history, attachment_text, **kwargs):
    return Notice(
        notice_type="NEW_DRIVE", company="Northwind Systems", role="Software Engineer",
        criteria=Criteria(min_gpa=7.0, gpa_inclusive=True, branches_allowed=["CSE"]),
        form_url="https://forms.gle/northwindSDE",
        evidence=[
            Evidence(field="company", quote="Northwind Systems"),
            Evidence(field="role", quote="Software Engineer"),
            Evidence(field="criteria.min_gpa", quote="CGPA 7.0 and above"),
            Evidence(field="criteria.gpa_inclusive", quote="CGPA 7.0 and above"),
            Evidence(field="criteria.branches_allowed", quote="CSE"),
            Evidence(field="form_url", quote="forms.gle/northwindSDE"),
        ],
    )


def _run_pipeline(tmp_path, *, sheets, autofill_form_fn):
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)

    mail = FakeMailSource()
    calendar = FakeCalendarStore()
    messenger = FakeMessenger()
    files = FakeFileStore([])
    ctx = run.PipelineContext(
        mail=mail, files=files, extract_fn=_stub_extract, api_key="unused", model="stub",
        timezone_name="Asia/Kolkata", db_path=db_path, use_llm_cache=False, calendar=calendar,
        sheets=sheets, autofill_form_fn=autofill_form_fn,
    )

    msg = EmailMessage(
        message_id="m1", thread_id="t1", from_addr="Career Office <careers.demo.college@gmail.com>",
        subject="Campus Drive: Northwind Systems - Software Engineer",
        body_text="CGPA 7.0 and above. CSE. Apply: https://forms.gle/northwindSDE", received_at=NOW,
    )
    run.process_message(msg, ctx, profile=PROFILE, policy=POLICY, now=NOW)
    executor.run_pending(db_path, Adapters(sheets=sheets, calendar=calendar, messenger=messenger))

    return [c[1]["text"] for c in messenger.calls if c[0] == "send"]


def test_autofill_result_wins_over_stored_template(tmp_path):
    sheets = FakeSheetStore(PROFILE, POLICY, form_templates={
        "https://forms.gle/northwindSDE": "https://docs.google.com/forms/d/e/TEMPLATE/viewform?entry.1=student_name",
    })

    def stub_autofill(form_url, profile, resume_text, jd_text, **kwargs):
        assert form_url == "https://forms.gle/northwindSDE"
        return "https://docs.google.com/forms/d/e/AUTOFILLED/viewform?entry.111=Riya+Mehta"

    sent_texts = _run_pipeline(tmp_path, sheets=sheets, autofill_form_fn=stub_autofill)
    approval_text = next(t for t in sent_texts if "Register?" in t)

    assert "AUTOFILLED" in approval_text
    assert "TEMPLATE" not in approval_text


def test_falls_back_to_stored_template_when_autofill_returns_none(tmp_path):
    sheets = FakeSheetStore(PROFILE, POLICY, form_templates={
        "https://forms.gle/northwindSDE": "https://docs.google.com/forms/d/e/TEMPLATE/viewform?entry.1=student_name",
    })

    sent_texts = _run_pipeline(tmp_path, sheets=sheets, autofill_form_fn=lambda *a, **k: None)
    approval_text = next(t for t in sent_texts if "Register?" in t)

    assert "TEMPLATE" in approval_text
    assert "Form (pre-filled, just review & submit):" in approval_text


def test_falls_back_to_plain_link_when_nothing_available(tmp_path):
    sheets = FakeSheetStore(PROFILE, POLICY)  # no FormTemplates entries

    sent_texts = _run_pipeline(tmp_path, sheets=sheets, autofill_form_fn=lambda *a, **k: None)
    approval_text = next(t for t in sent_texts if "Register?" in t)

    assert "Form: https://forms.gle/northwindSDE" in approval_text
    assert "pre-filled" not in approval_text


def test_default_context_never_attempts_autofill(tmp_path):
    """autofill_form_fn defaults to None — a context built without it (every
    fixture-driven eval scenario, every other test) must behave exactly as
    it did before this feature existed."""
    sheets = FakeSheetStore(PROFILE, POLICY, form_templates={
        "https://forms.gle/northwindSDE": "https://docs.google.com/forms/d/e/TEMPLATE/viewform?entry.1=student_name",
    })

    sent_texts = _run_pipeline(tmp_path, sheets=sheets, autofill_form_fn=None)
    approval_text = next(t for t in sent_texts if "Register?" in t)

    assert "TEMPLATE" in approval_text  # falls straight to the stored template, no autofill attempted


def test_autofill_uses_generated_resume_local_content(tmp_path):
    from unittest.mock import patch
    from cutoff.models import Drive
    from cutoff.pipeline import resolve

    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)

    gen_dir = tmp_path / "gen_resumes"
    gen_dir.mkdir()
    pdf_file = gen_dir / "gen123.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 test resume content")

    mail = FakeMailSource()
    calendar = FakeCalendarStore()
    messenger = FakeMessenger()
    files = FakeFileStore([])  # empty files store; calling get_resume_content would fail!

    captured = {}

    def stub_autofill(form_url, profile, resume_text, jd_text, **kwargs):
        captured["resume_text"] = resume_text
        captured["master_profile"] = kwargs.get("master_profile")
        return "https://docs.google.com/forms/d/e/AUTOFILLED/viewform?entry.1=ok"

    ctx = run.PipelineContext(
        mail=mail, files=files, extract_fn=_stub_extract, api_key="unused", model="stub",
        timezone_name="Asia/Kolkata", db_path=db_path, use_llm_cache=False, calendar=calendar,
        sheets=FakeSheetStore(PROFILE, POLICY),
        autofill_form_fn=stub_autofill,
        generated_resume_dir=str(gen_dir),
    )

    drive = Drive(
        drive_id="northwind-systems:software-engineer:2026",
        company="Northwind Systems",
        role="Software Engineer",
        batch_year=2026,
        status="OPEN",
        form_url="https://forms.gle/northwindSDE",
        resolved_resume_pick={
            "resolved": True,
            "file_id": "generated:gen123.pdf",
            "name": "gen123.pdf",
            "web_view_link": "http://localhost/gen123.pdf",
            "reason": "Tailored",
        },
    )
    resolve.save_drive(db_path, drive)

    msg = EmailMessage(
        message_id="m1", thread_id="t1", from_addr="Career Office <careers.demo.college@gmail.com>",
        subject="Campus Drive: Northwind Systems - Software Engineer",
        body_text="CGPA 7.0 and above. CSE. Apply: https://forms.gle/northwindSDE", received_at=NOW,
    )

    with patch("cutoff.pipeline.ingest.extract_pdf_text", return_value="Extracted PDF Text"):
        run.process_message(msg, ctx, profile=PROFILE, policy=POLICY, now=NOW)

    assert captured.get("resume_text") == "Extracted PDF Text"

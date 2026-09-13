"""End-to-end: a stored FormTemplates entry (Section 6.5 extension) reaches
the Telegram approval card as a pre-filled link with the student's own
profile substituted in; with no template on file, the plain form_url is used
instead — same as before this feature existed. Never calls a real LLM,
Sheets, or Forms API (Section 0 rule 3)."""
from datetime import datetime, timezone

from cutoff import db
from cutoff.adapters.fakes import FakeCalendarStore, FakeFileStore, FakeMailSource, FakeMessenger, FakeSheetStore
from cutoff.models import CollegePolicy, Criteria, EmailMessage, Evidence, Notice, StudentProfile
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


def _run_pipeline(tmp_path, sheets):
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)

    mail = FakeMailSource()
    calendar = FakeCalendarStore()
    messenger = FakeMessenger()
    files = FakeFileStore([])
    ctx = run.PipelineContext(
        mail=mail, files=files, extract_fn=_stub_extract, api_key="unused", model="stub",
        timezone_name="Asia/Kolkata", db_path=db_path, use_llm_cache=False, calendar=calendar, sheets=sheets,
    )

    msg = EmailMessage(
        message_id="m1", thread_id="t1", from_addr="Career Office <careers.demo.college@gmail.com>",
        subject="Campus Drive: Northwind Systems - Software Engineer",
        body_text="CGPA 7.0 and above. CSE. Apply: https://forms.gle/northwindSDE", received_at=NOW,
    )
    run.process_message(msg, ctx, profile=PROFILE, policy=POLICY, now=NOW)
    executor.run_pending(db_path, Adapters(sheets=sheets, calendar=calendar, messenger=messenger))

    return [c[1]["text"] for c in messenger.calls if c[0] == "send"]


def test_prefilled_link_used_when_template_on_file(tmp_path):
    sheets = FakeSheetStore(PROFILE, POLICY, form_templates={
        "https://forms.gle/northwindSDE": (
            "https://docs.google.com/forms/d/e/X/viewform?usp=pp_url"
            "&entry.111=student_name&entry.222=student_roll_no"
        ),
    })

    sent_texts = _run_pipeline(tmp_path, sheets)
    approval_text = next(t for t in sent_texts if "Register?" in t)

    assert "Form (pre-filled, just review & submit):" in approval_text
    assert "Riya+Mehta" in approval_text or "Riya%20Mehta" in approval_text or "Riya Mehta" in approval_text
    assert "21BCS045" in approval_text
    assert "student_name" not in approval_text  # placeholder tokens must not leak through


def test_plain_link_used_when_no_template_on_file(tmp_path):
    sheets = FakeSheetStore(PROFILE, POLICY)  # no FormTemplates entries

    sent_texts = _run_pipeline(tmp_path, sheets)
    approval_text = next(t for t in sent_texts if "Register?" in t)

    assert "Form: https://forms.gle/northwindSDE" in approval_text
    assert "pre-filled" not in approval_text

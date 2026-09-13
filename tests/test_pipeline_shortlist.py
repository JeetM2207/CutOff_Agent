"""End-to-end: a SHORTLIST notice for an existing drive must run shortlist
matching and reach the planner, even though it changes none of Drive's own
fields (Section 13) — this is exactly the early-return bug fixed in run.py.
Uses a stubbed extract_fn and the real generated PDF fixture; never the real LLM."""
from datetime import datetime, timezone
from pathlib import Path

from cutoff import db
from cutoff.adapters.fakes import FakeCalendarStore, FakeFileStore, FakeMailSource, FakeMessenger, FakeSheetStore
from cutoff.models import AttachmentMeta, CollegePolicy, Criteria, EmailMessage, Evidence, Notice, StudentProfile
from cutoff.pipeline import executor, resolve, run
from cutoff.pipeline.executor import Adapters

PDF_PATH = (
    Path(__file__).resolve().parent.parent / "eval" / "fixtures" / "dev"
    / "dev_022_shortlist_included_clean" / "attachments" / "shortlist.pdf"
)


def _stub_extract(msg: EmailMessage, body_clean: str, quoted_history: str, attachment_text: str, **kwargs) -> Notice:
    if msg.message_id == "m1":
        return Notice(
            notice_type="NEW_DRIVE", company="Cobalt Freight", role="Operations Analyst",
            criteria=Criteria(min_gpa=7.0, gpa_inclusive=True, branches_allowed=["CSE"]),
            evidence=[
                Evidence(field="company", quote="Cobalt Freight"),
                Evidence(field="role", quote="Operations Analyst"),
                Evidence(field="criteria.min_gpa", quote="CGPA 7.0 and above"),
                Evidence(field="criteria.gpa_inclusive", quote="CGPA 7.0 and above"),
                Evidence(field="criteria.branches_allowed", quote="CSE"),
            ],
        )
    if msg.message_id == "m2":
        return Notice(notice_type="SHORTLIST")  # a shortlist email states nothing about criteria
    raise AssertionError(f"unexpected message_id {msg.message_id!r}")


def test_shortlist_notice_with_no_field_changes_still_runs_matching(tmp_path):
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)

    profile = StudentProfile(
        name="Riya Mehta", roll_no="21BCS045", email="riya@college.edu", branch="CSE",
        gpa=7.42, active_backlogs=0, batch_year=2026,
    )
    policy = CollegePolicy(college_domain="college.edu", career_office_senders=["careers.demo.college@gmail.com"])
    now = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)

    mail = FakeMailSource()
    sheets = FakeSheetStore(profile, policy)
    calendar = FakeCalendarStore()
    messenger = FakeMessenger()
    files = FakeFileStore([])

    ctx = run.PipelineContext(
        mail=mail, files=files, extract_fn=_stub_extract, api_key="unused", model="stub",
        timezone_name="Asia/Kolkata", db_path=db_path, use_llm_cache=False, calendar=calendar,
    )

    m1 = EmailMessage(
        message_id="m1", thread_id="t1", from_addr="Career Office <careers.demo.college@gmail.com>",
        subject="Campus Drive: Cobalt Freight - Operations Analyst", body_text="CGPA 7.0 and above. CSE.",
        received_at=now,
    )
    run.process_message(m1, ctx, profile=profile, policy=policy, now=now)

    pdf_bytes = PDF_PATH.read_bytes()
    attachment = AttachmentMeta(attachment_id="att1", filename="shortlist.pdf", mime_type="application/pdf")
    m2 = EmailMessage(
        message_id="m2", thread_id="t1", from_addr="Career Office <careers.demo.college@gmail.com>",
        subject="SHORTLIST: Cobalt Freight", body_text="Shortlist attached.",
        received_at=now, attachments=[attachment],
    )
    mail.deliver(m2, {"att1": pdf_bytes})
    result = run.process_message(m2, ctx, profile=profile, policy=policy, now=now)

    assert result.note == "ok"  # not "shortlist: no changes" — the bug this test guards against
    assert result.actions_planned > 0

    executor.run_pending(db_path, Adapters(sheets=sheets, calendar=calendar, messenger=messenger))

    drive_id = "cobalt-freight:operations-analyst:2026"
    row = sheets.read_drive_row(drive_id)
    assert row is not None
    assert row["status"] == "SHORTLISTED"

    sent_texts = [c[1]["text"] for c in messenger.calls if c[0] == "send"]
    assert any("shortlist" in t.lower() and "page 1" in t for t in sent_texts)

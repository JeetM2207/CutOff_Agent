"""End-to-end: JD-content resume matching (Section 6.4 extension) only ever
kicks in for an ELIGIBLE NEW_DRIVE/REVISION — never for a SHORTLIST notice's
own PDF attachment, which is extracted into the same ingested.attachment_text
but is not a job description. Never calls the real LLM or Drive API
(Section 0 rule 3): resume_match.select_best_resume and both extract_pdf_text
call sites are stubbed."""
from datetime import datetime, timezone
from types import SimpleNamespace

from cutoff import db
from cutoff.adapters.fakes import FakeCalendarStore, FakeFileStore, FakeMailSource, FakeMessenger, FakeSheetStore
from cutoff.bot.telegram_loop import TelegramBotLoop
from cutoff.models import (
    AttachmentMeta, CollegePolicy, Criteria, EmailMessage, Evidence, MasterProfile, MasterProfileProject,
    Notice, ResumeFile, StudentProfile,
)
from cutoff.pipeline import executor, ingest, resume, run
from cutoff.pipeline.executor import Adapters

PROFILE = StudentProfile(
    name="Riya Mehta", roll_no="21BCS045", email="riya@college.edu", branch="CSE",
    gpa=7.42, active_backlogs=0, batch_year=2026,
)
POLICY = CollegePolicy(college_domain="college.edu", career_office_senders=["careers.demo.college@gmail.com"])
NOW = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)


def _ctx(db_path, mail, files, extract_fn, calendar):
    return run.PipelineContext(
        mail=mail, files=files, extract_fn=extract_fn, api_key="unused", model="stub",
        timezone_name="Asia/Kolkata", db_path=db_path, use_llm_cache=False, calendar=calendar,
        provider="anthropic",
    )


def test_shortlist_pdf_attachment_never_triggers_resume_matching(tmp_path, monkeypatch):
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)
    monkeypatch.setattr(ingest, "extract_pdf_text", lambda data: "roll numbers: 21BCS045")

    def stub_extract(msg, body_clean, quoted_history, attachment_text, **kwargs):
        return Notice(notice_type="SHORTLIST")

    files = FakeFileStore([ResumeFile(file_id="f1", name="resume_SDE.pdf", web_view_link="https://drive/sde")])
    mail = FakeMailSource()
    ctx = _ctx(db_path, mail, files, stub_extract, FakeCalendarStore())

    attachment = AttachmentMeta(attachment_id="att1", filename="shortlist.pdf", mime_type="application/pdf")
    msg = EmailMessage(
        message_id="m1", thread_id="t1", from_addr="Career Office <careers.demo.college@gmail.com>",
        subject="SHORTLIST: Some Drive", body_text="Shortlist attached.", received_at=NOW, attachments=[attachment],
    )
    mail.deliver(msg, {"att1": b"fake-pdf-bytes"})

    run.process_message(msg, ctx, profile=PROFILE, policy=POLICY, now=NOW)

    assert "get_resume_content" not in [c[0] for c in files.calls]


def test_eligible_new_drive_with_jd_attachment_uses_smart_match(tmp_path, monkeypatch):
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)

    monkeypatch.setattr(ingest, "extract_pdf_text", lambda data: "Looking for a backend engineer with SQL experience.")
    monkeypatch.setattr(resume, "extract_pdf_text", lambda data: f"resume content: {data.decode()}")

    def fake_select_best_resume(jd_text, resumes, **kwargs):
        assert "backend engineer" in jd_text
        return "resume_SDE.pdf", "Lists backend + SQL experience matching the JD."

    monkeypatch.setattr("cutoff.llm.resume_match.select_best_resume", fake_select_best_resume)

    def stub_extract(msg, body_clean, quoted_history, attachment_text, **kwargs):
        return Notice(
            notice_type="NEW_DRIVE", company="Northwind Systems", role="Software Engineer",
            criteria=Criteria(min_gpa=7.0, gpa_inclusive=True, branches_allowed=["CSE"]),
            role_category="SDE",
            evidence=[
                Evidence(field="company", quote="Northwind Systems"),
                Evidence(field="role", quote="Software Engineer"),
                Evidence(field="criteria.min_gpa", quote="CGPA 7.0 and above"),
                Evidence(field="criteria.gpa_inclusive", quote="CGPA 7.0 and above"),
                Evidence(field="criteria.branches_allowed", quote="CSE"),
            ],
        )

    files = FakeFileStore(
        [
            ResumeFile(file_id="f_sde", name="resume_SDE.pdf", web_view_link="https://drive/sde"),
            ResumeFile(file_id="f_default", name="resume_DEFAULT.pdf", web_view_link="https://drive/default"),
        ],
        contents={"f_sde": b"sde-bytes", "f_default": b"default-bytes"},
    )
    mail = FakeMailSource()
    calendar = FakeCalendarStore()
    sheets = FakeSheetStore(PROFILE, POLICY)
    messenger = FakeMessenger()
    ctx = _ctx(db_path, mail, files, stub_extract, calendar)

    attachment = AttachmentMeta(attachment_id="jd1", filename="job_description.pdf", mime_type="application/pdf")
    msg = EmailMessage(
        message_id="m1", thread_id="t1", from_addr="Career Office <careers.demo.college@gmail.com>",
        subject="Campus Drive: Northwind Systems - Software Engineer",
        body_text="CGPA 7.0 and above. CSE.", received_at=NOW, attachments=[attachment],
    )
    mail.deliver(msg, {"jd1": b"job-description-pdf-bytes"})

    result = run.process_message(msg, ctx, profile=PROFILE, policy=POLICY, now=NOW)
    assert result.verdict == "ELIGIBLE"

    executor.run_pending(db_path, Adapters(sheets=sheets, calendar=calendar, messenger=messenger))

    sent_texts = [c[1]["text"] for c in messenger.calls if c[0] == "send"]
    approval_text = next(t for t in sent_texts if "Register?" in t)
    assert "drive/sde" in approval_text
    assert "Lists backend + SQL experience matching the JD." in approval_text


def test_eligible_new_drive_asks_which_resume_then_generates_one_on_request(tmp_path, monkeypatch):
    """Dynamic resume generation (Section 6.4 extension), wired all the way
    through: an ELIGIBLE drive with a master profile configured sends a
    resume-CHOICE card first (never decides silently); tapping "Generate
    tailored resume" resolves it into the real Register/Skip/Remind card,
    edited in place under the same message. Never calls a real LLM: both the
    tailoring call and the PDF renderer are stubbed."""
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)

    monkeypatch.setattr(ingest, "extract_pdf_text", lambda data: "Looking for a backend engineer with Django experience.")

    def fake_generate_tailored_resume(jd_text, master_profile, **kwargs):
        assert "backend engineer" in jd_text
        assert "Django" in master_profile.skills
        sections = {
            "headline": "Backend-focused CSE student.", "skills": ["Python", "Django"],
            "highlighted_projects": [{"title": "Order Service", "bullets": ["Built a Django REST API."]}],
            "match_reason": "Emphasized Django experience matching the JD's backend stack.",
        }
        return sections, sections["match_reason"]

    monkeypatch.setattr("cutoff.llm.resume_generate.generate_tailored_resume", fake_generate_tailored_resume)
    monkeypatch.setattr("cutoff.pipeline.resume_pdf.render_resume_pdf", lambda *a, **k: b"%PDF-1.4 fake bytes")

    def stub_extract(msg, body_clean, quoted_history, attachment_text, **kwargs):
        return Notice(
            notice_type="NEW_DRIVE", company="Northwind Systems", role="Software Engineer",
            criteria=Criteria(min_gpa=7.0, gpa_inclusive=True, branches_allowed=["CSE"]),
            role_category="SDE",
            evidence=[
                Evidence(field="company", quote="Northwind Systems"),
                Evidence(field="role", quote="Software Engineer"),
                Evidence(field="criteria.min_gpa", quote="CGPA 7.0 and above"),
                Evidence(field="criteria.gpa_inclusive", quote="CGPA 7.0 and above"),
                Evidence(field="criteria.branches_allowed", quote="CSE"),
            ],
        )

    master_profile = MasterProfile(
        skills=["Python", "Django"],
        projects=[MasterProfileProject(title="Order Service", bullets=["Built a Django REST API."])],
    )
    files = FakeFileStore([])  # generation needs no pre-existing Drive resume at all
    mail = FakeMailSource()
    calendar = FakeCalendarStore()
    sheets = FakeSheetStore(PROFILE, POLICY)
    messenger = FakeMessenger()
    ctx = run.PipelineContext(
        mail=mail, files=files, extract_fn=stub_extract, api_key="unused", model="stub",
        timezone_name="Asia/Kolkata", db_path=db_path, use_llm_cache=False, calendar=calendar,
        provider="anthropic",
        master_profile=master_profile,
        generated_resume_dir=str(tmp_path / "generated"), public_base_url="http://127.0.0.1:8000",
    )

    attachment = AttachmentMeta(attachment_id="jd1", filename="job_description.pdf", mime_type="application/pdf")
    msg = EmailMessage(
        message_id="m1", thread_id="t1", from_addr="Career Office <careers.demo.college@gmail.com>",
        subject="Campus Drive: Northwind Systems - Software Engineer",
        body_text="CGPA 7.0 and above. CSE.", received_at=NOW, attachments=[attachment],
    )
    mail.deliver(msg, {"jd1": b"job-description-pdf-bytes"})

    result = run.process_message(msg, ctx, profile=PROFILE, policy=POLICY, now=NOW)
    assert result.verdict == "ELIGIBLE"

    exec_adapters = Adapters(sheets=sheets, calendar=calendar, messenger=messenger, files=files)
    executor.run_pending(db_path, exec_adapters)

    # Step 1: the FIRST message is a resume choice, not the direct approval.
    sent_texts = [c[1]["text"] for c in messenger.calls if c[0] == "send"]
    choice_text = next(t for t in sent_texts if "Want me to use your resume" in t)
    assert "Register?" not in choice_text
    approval = executor.get_pending_approval(db_path, result.drive_id)
    assert approval is not None

    # Step 2: tapping "Generate tailored resume" resolves it in place.
    settings = SimpleNamespace(
        llm_api_key="unused", llm_model="stub", llm_provider="anthropic", llm_base_url=None,
        generated_resume_dir=str(tmp_path / "generated"), public_base_url="http://127.0.0.1:8000",
        master_profile_path=str(tmp_path / "unused_master_profile.yaml"),
    )
    monkeypatch.setattr("cutoff.pipeline.master_profile.load_master_profile", lambda path: master_profile)
    bot = TelegramBotLoop("token", "chat1", db_path, exec_adapters, settings=settings)
    bot._handle_resume_choice(approval, "generate")

    edit_calls = [c for c in messenger.calls if c[0] == "edit"]
    assert len(edit_calls) == 1  # the choice card was edited in place, not sent as a second message
    approval_text = edit_calls[0][1]["text"]
    assert "Register?" in approval_text
    assert "/generated_resumes/" in approval_text
    assert "Generated bespoke resume" in approval_text
    assert "Emphasized Django experience matching the JD's backend stack." in approval_text
    assert list((tmp_path / "generated").iterdir())  # the PDF was actually written to disk

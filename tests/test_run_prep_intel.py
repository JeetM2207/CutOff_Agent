"""End-to-end: interview-prep intel (new extension) wired through
PipelineContext -> run.process_message -> the Telegram approval card.
Never calls a real LLM or search API — cutoff.pipeline.prep_intel.
fetch_prep_intel is stubbed. Confirms the opt-in gate (off by default) and
that a drive is only ever searched once, never re-searched on a later
revision replan."""
from datetime import datetime, timezone

from cutoff import db
from cutoff.adapters.fakes import FakeCalendarStore, FakeFileStore, FakeMailSource, FakeMessenger, FakeSheetStore
from cutoff.models import CollegePolicy, Criteria, EmailMessage, Evidence, Notice, StudentProfile
from cutoff.pipeline import executor, resolve, run
from cutoff.pipeline.executor import Adapters

PROFILE = StudentProfile(
    name="Riya Mehta", roll_no="21BCS045", email="riya@college.edu", branch="CSE",
    gpa=7.42, active_backlogs=0, batch_year=2026,
)
POLICY = CollegePolicy(college_domain="college.edu", career_office_senders=["careers.demo.college@gmail.com"])
NOW = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)


def _stub_extract(msg, body_clean, quoted_history, attachment_text, **kwargs):
    return Notice(
        notice_type="NEW_DRIVE", company="Zentrix Analytics", role="Software Engineer",
        criteria=Criteria(min_gpa=7.0, gpa_inclusive=True, branches_allowed=["CSE"]),
        evidence=[
            Evidence(field="company", quote="Zentrix Analytics"), Evidence(field="role", quote="Software Engineer"),
            Evidence(field="criteria.min_gpa", quote="CGPA 7.0 and above"),
            Evidence(field="criteria.gpa_inclusive", quote="CGPA 7.0 and above"),
            Evidence(field="criteria.branches_allowed", quote="CSE"),
        ],
    )


def _ctx(db_path, mail, files, calendar, *, enable_prep_intel):
    return run.PipelineContext(
        mail=mail, files=files, extract_fn=_stub_extract, api_key="unused", model="stub",
        timezone_name="Asia/Kolkata", db_path=db_path, use_llm_cache=False, calendar=calendar,
        provider="anthropic", enable_prep_intel=enable_prep_intel,
    )


def _message(message_id="m1", thread_id="t1", subject="Campus Drive: Zentrix Analytics - Software Engineer"):
    return EmailMessage(
        message_id=message_id, thread_id=thread_id, from_addr="Career Office <careers.demo.college@gmail.com>",
        subject=subject, body_text="CGPA 7.0 and above. CSE.", received_at=NOW,
    )


def test_prep_intel_is_never_fetched_when_disabled(tmp_path, monkeypatch):
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)
    called = []
    monkeypatch.setattr("cutoff.pipeline.prep_intel.fetch_prep_intel", lambda *a, **k: called.append(1))

    mail, calendar = FakeMailSource(), FakeCalendarStore()
    ctx = _ctx(db_path, mail, FakeFileStore([]), calendar, enable_prep_intel=False)
    msg = _message()
    mail.deliver(msg)

    result = run.process_message(msg, ctx, profile=PROFILE, policy=POLICY, now=NOW)

    assert result.verdict == "ELIGIBLE"
    assert not called
    assert resolve.get_drive(db_path, result.drive_id).prep_intel is None


def test_prep_intel_is_fetched_once_and_shown_on_the_approval_card(tmp_path, monkeypatch):
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)
    call_count = []

    def fake_fetch(company, role, **kwargs):
        call_count.append((company, role))
        return {"attempted": True, "strategy_summary": "Expect graph and DP questions.",
                "top_reference_links": ["https://leetcode.com/discuss/1"]}

    monkeypatch.setattr("cutoff.pipeline.prep_intel.fetch_prep_intel", fake_fetch)

    mail, calendar, sheets, messenger = FakeMailSource(), FakeCalendarStore(), FakeSheetStore(PROFILE, POLICY), FakeMessenger()
    ctx = _ctx(db_path, mail, FakeFileStore([]), calendar, enable_prep_intel=True)
    msg = _message()
    mail.deliver(msg)

    result = run.process_message(msg, ctx, profile=PROFILE, policy=POLICY, now=NOW)
    executor.run_pending(db_path, Adapters(sheets=sheets, calendar=calendar, messenger=messenger))

    assert call_count == [("Zentrix Analytics", "Software Engineer")]
    saved = resolve.get_drive(db_path, result.drive_id)
    assert saved.prep_intel["strategy_summary"] == "Expect graph and DP questions."

    sent_texts = [c[1]["text"] for c in messenger.calls if c[0] == "send"]
    approval_text = next(t for t in sent_texts if "Register?" in t)
    assert "💡 Prep Strategy: Expect graph and DP questions." in approval_text
    assert "🔗 Study materials: https://leetcode.com/discuss/1" in approval_text


def test_prep_intel_is_not_re_fetched_on_a_later_revision(tmp_path, monkeypatch):
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)
    call_count = []

    def fake_fetch(company, role, **kwargs):
        call_count.append(1)
        return {"attempted": True, "strategy_summary": "Expect graph questions.", "top_reference_links": ["https://leetcode.com/discuss/1"]}

    monkeypatch.setattr("cutoff.pipeline.prep_intel.fetch_prep_intel", fake_fetch)

    mail, calendar = FakeMailSource(), FakeCalendarStore()
    ctx = _ctx(db_path, mail, FakeFileStore([]), calendar, enable_prep_intel=True)
    msg1 = _message()
    mail.deliver(msg1)
    result1 = run.process_message(msg1, ctx, profile=PROFILE, policy=POLICY, now=NOW)
    assert len(call_count) == 1

    def _stub_extract_revision(msg, body_clean, quoted_history, attachment_text, **kwargs):
        return Notice(
            notice_type="REVISION", company="Zentrix Analytics", role="Software Engineer",
            criteria=Criteria(min_gpa=7.5, gpa_inclusive=True, branches_allowed=["CSE"]),
            evidence=[
                Evidence(field="company", quote="Zentrix Analytics"), Evidence(field="role", quote="Software Engineer"),
                Evidence(field="criteria.min_gpa", quote="CGPA 7.5 and above"),
                Evidence(field="criteria.gpa_inclusive", quote="CGPA 7.5 and above"),
                Evidence(field="criteria.branches_allowed", quote="CSE"),
            ],
        )
    ctx.extract_fn = _stub_extract_revision
    msg2 = _message(message_id="m2", thread_id="t1")
    mail.deliver(msg2)
    result2 = run.process_message(msg2, ctx, profile=PROFILE, policy=POLICY, now=NOW)

    assert result2.drive_id == result1.drive_id
    assert len(call_count) == 1  # NOT re-fetched on the revision

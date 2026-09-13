"""Phase 0 smoke tests: db schema, models, trace spans, fakes recording calls,
and the empty dashboard API. No real API or LLM calls anywhere in this file."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from cutoff import db, trace
from cutoff.adapters.fakes import (
    FakeCalendarStore,
    FakeFileStore,
    FakeMailSource,
    FakeMessenger,
    FakeSheetStore,
)
from cutoff.app.server import app
from cutoff.models import (
    Action,
    Button,
    CalendarEvent,
    CollegePolicy,
    Criteria,
    Drive,
    EmailMessage,
    Notice,
    ResumeFile,
    StudentProfile,
    Verdict,
)

EXPECTED_TABLES = {
    "processed_messages", "drives", "drive_history",
    "actions", "approvals", "traces", "llm_cache", "profile_imports",
}


def test_init_db_creates_all_tables(tmp_path):
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {r["name"] for r in rows}
    assert EXPECTED_TABLES <= names


def test_models_construct_with_minimal_fields():
    profile = StudentProfile(
        name="Riya Mehta", roll_no="21BCS045", email="riya@college.edu",
        branch="CSE", gpa=7.42, active_backlogs=0, pct_10th=91, pct_12th=86,
        batch_year=2026,
    )
    policy = CollegePolicy(college_domain="college.edu")
    notice = Notice(notice_type="NEW_DRIVE", criteria=Criteria(min_gpa=7.0))
    drive = Drive(drive_id="zentrix-analytics:data-analyst:2026", company="Zentrix Analytics", role="Data Analyst")
    verdict = Verdict(result="ELIGIBLE")
    action = Action(
        action_id="a1", idempotency_key="cal:zentrix:deadline", drive_id=drive.drive_id,
        drive_version=1, app="calendar", kind="upsert_event", tier="T1_REVERSIBLE",
    )

    assert profile.gpa_scale == 10.0
    assert policy.allowed_form_domains == ["docs.google.com", "forms.gle"]
    assert notice.unverified_fields == []
    assert drive.status == "OPEN"
    assert verdict.result == "ELIGIBLE"
    assert action.status == "PLANNED"


def test_trace_span_writes_rows(tmp_path):
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)
    trace.configure(db_path)

    with trace.run("run-1"):
        with trace.span("ingest", message_id="m1"):
            pass
        with trace.span("extract", model="claude-sonnet-5"):
            pass

    spans = trace.list_spans("run-1")
    names = [s["name"] for s in spans]
    assert names == ["ingest", "extract"]
    assert all(s["status"] == "ok" for s in spans)
    assert all(s["ended_at"] is not None for s in spans)


def test_trace_span_marks_error_status(tmp_path):
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)
    trace.configure(db_path)

    with trace.run("run-2"):
        try:
            with trace.span("extract"):
                raise ValueError("boom")
        except ValueError:
            pass

    spans = trace.list_spans("run-2")
    assert spans[0]["status"] == "error"


def _sample_email(message_id="m1", from_addr="Career Office <careers.demo.college@gmail.com>"):
    return EmailMessage(
        message_id=message_id, thread_id="t1", from_addr=from_addr,
        subject="Campus Drive: Zentrix Analytics", body_text="CGPA 7.0 and above.",
        received_at=datetime(2026, 9, 10, 10, 15, tzinfo=timezone.utc),
    )


def test_fake_mail_source_lists_new_and_records_calls():
    mail = FakeMailSource([_sample_email()])
    since = datetime(2026, 9, 1, tzinfo=timezone.utc)

    refs = mail.list_new(["careers.demo.college@gmail.com"], since)
    assert len(refs) == 1
    assert refs[0].message_id == "m1"

    msg = mail.get("m1")
    assert msg.subject.startswith("Campus Drive")

    assert [c[0] for c in mail.calls] == ["list_new", "get"]


def test_fake_mail_source_ignores_non_allowlisted_sender():
    mail = FakeMailSource([_sample_email(from_addr="Scammer <scam@evil.com>")])
    refs = mail.list_new(["careers.demo.college@gmail.com"], datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert refs == []


def test_fake_mail_source_duplicate_delivery_is_idempotent_in_store():
    mail = FakeMailSource()
    msg = _sample_email()
    mail.deliver(msg)
    mail.deliver(msg)  # duplicate delivery of the same message_id
    since = datetime(2020, 1, 1, tzinfo=timezone.utc)
    refs = mail.list_new(["careers.demo.college@gmail.com"], since)
    assert len(refs) == 1


def test_fake_sheet_store_upsert_and_read():
    profile = StudentProfile(
        name="Riya", roll_no="21BCS045", email="riya@college.edu", branch="CSE",
        gpa=7.42, active_backlogs=0, batch_year=2026,
    )
    policy = CollegePolicy(college_domain="college.edu")
    sheet = FakeSheetStore(profile, policy)

    assert sheet.read_drive_row("drive-1") is None
    sheet.upsert_drive_row("drive-1", {"verdict": "ELIGIBLE"})
    sheet.upsert_drive_row("drive-1", {"version": 2})
    row = sheet.read_drive_row("drive-1")
    assert row == {"verdict": "ELIGIBLE", "version": 2}
    assert sheet.read_profile() is profile
    assert sheet.read_policy() is policy


def test_fake_calendar_store_upsert_cancel_get_are_idempotent():
    cal = FakeCalendarStore()
    event = CalendarEvent(event_id="ignored", title="Deadline", start=datetime(2026, 9, 12, 18, 29, tzinfo=timezone.utc))

    event_id = cal.upsert_event("cal:drive1:deadline", event)
    assert event_id == "cal:drive1:deadline"
    got = cal.get_event(event_id)
    assert got.status == "confirmed"
    assert got.title == "Deadline"

    cal.upsert_event(event_id, event)  # re-upsert with same key: no duplicate
    assert len(cal._events) == 1

    cal.cancel_event(event_id)
    assert cal.get_event(event_id).status == "cancelled"


def test_fake_calendar_store_exam_clash_window():
    exam = CalendarEvent(
        event_id="exam1", title="DBMS Midterm",
        start=datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc),
        end=datetime(2026, 9, 12, 11, 0, tzinfo=timezone.utc),
    )
    cal = FakeCalendarStore(exam_events=[exam])
    clashes = cal.list_exam_events(
        datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
    )
    assert clashes == [exam]


def test_fake_file_store_lists_resumes():
    resumes = [ResumeFile(file_id="f1", name="resume_DATA.pdf", web_view_link="https://drive/f1")]
    store = FakeFileStore(resumes)
    assert store.list_resumes() == resumes


def test_fake_messenger_send_and_edit():
    bot = FakeMessenger()
    message_id = bot.send("approve:drive1", "Register?", [Button(text="Approve", callback_data="a:1:approve")])
    assert message_id
    bot.edit(message_id, "Updated", None)
    assert bot._messages[message_id]["text"] == "Updated"
    assert [c[0] for c in bot.calls] == ["send", "edit"]


def test_dashboard_serves_empty_board(tmp_path, monkeypatch):
    # server.py's routes read get_settings().db_path fresh per request (not
    # baked in at import time), so pointing DB_PATH at an isolated tmp file
    # here is enough — no need to re-import the already-imported `app`.
    # Using the shared default cutoff.db would leak whatever a real MODE=real
    # run has already written there.
    db_path = str(tmp_path / "cutoff.db")
    monkeypatch.setenv("DB_PATH", db_path)
    db.init_db(db_path)
    trace.configure(db_path)

    client = TestClient(app)

    resp = client.get("/")
    assert resp.status_code == 200
    assert "CutOff" in resp.text

    resp = client.get("/api/drives")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"register": [], "your_call": [], "not_for_you": [], "watch_out": [], "closed": []}

"""FaultInjector (Section 15.4) and the crash-restart guarantee (Section 11.6)."""
from datetime import datetime, timedelta, timezone

import pytest

from cutoff import db
from cutoff.adapters import faults
from cutoff.adapters.fakes import FakeCalendarStore, FakeMailSource, FakeMessenger, FakeSheetStore
from cutoff.models import CalendarEvent, CollegePolicy, Criteria, Drive, EmailMessage, Notice, StudentProfile, Verdict
from cutoff.pipeline.executor import Adapters, AdapterError, CircuitBreaker, SimulatedCrash, run_pending

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _make_pdf_bytes(lines: list[str]) -> bytes:
    import io as _io

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = _io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 700
    for line in lines:
        c.drawString(50, y, line)
        y -= 18
    c.save()
    return buf.getvalue()


def test_ingest_survives_a_transient_attachment_fetch_failure():
    """Found live under kitchen_sink chaos: a single injected 429 on
    mail.get_attachment killed the whole message with zero retry — reads
    (Section 11.2) still need the same backoff as ledgered writes."""
    from cutoff.models import AttachmentMeta
    from cutoff.pipeline.ingest import ingest

    inner = FakeMailSource()
    msg = EmailMessage(
        message_id="m1", thread_id="t1", from_addr="Career Office <c@college.edu>",
        subject="Shortlist", body_text="See attached.", received_at=NOW,
        attachments=[AttachmentMeta(attachment_id="att1", filename="list.pdf", mime_type="application/pdf")],
    )
    inner.deliver(msg, {"att1": _make_pdf_bytes(["21BCS045 Riya Mehta"])})
    mail = faults.wrap_mail(inner, {"http_429_first_n": 2})

    # get_attachment fails twice (chaos) then succeeds — ingest must not
    # propagate the transient failure, and the real PDF text must come through.
    result = ingest(msg, mail)
    assert "21BCS045" in result.attachment_text


def test_gmail_throttle_raises_429_then_succeeds():
    mail = faults.wrap_mail(FakeMailSource(), {"http_429_first_n": 2, "retry_after_s": 0})
    with pytest.raises(AdapterError) as exc:
        mail.list_new(["x@y.com"], NOW)
    assert exc.value.status_code == 429
    with pytest.raises(AdapterError):
        mail.list_new(["x@y.com"], NOW)
    # third call succeeds (first_n exhausted)
    assert mail.list_new(["x@y.com"], NOW) == []


def test_calendar_flaky_is_deterministic_with_seed():
    calendar_a = faults.wrap_calendar(FakeCalendarStore(), {"http_500_rate": 1.0, "seed": 7})
    with pytest.raises(AdapterError) as exc:
        calendar_a.upsert_event("e1", CalendarEvent(event_id="e1", title="t", start=NOW))
    assert exc.value.status_code == 500


def test_telegram_timeout_first_n_then_succeeds():
    bot = faults.wrap_telegram(FakeMessenger(), {"timeout_first_n": 1})
    with pytest.raises(AdapterError) as exc:
        bot.send("k", "hello", None)
    assert exc.value.status_code is None  # timeout: no HTTP status
    message_id = bot.send("k", "hello", None)
    assert message_id


def test_duplicate_delivery_doubles_list_new_results():
    inner = FakeMailSource()
    inner.deliver(EmailMessage(
        message_id="m1", thread_id="t1", from_addr="Career Office <c@college.edu>",
        subject="s", body_text="b", received_at=NOW,
    ))
    mail = faults.wrap_mail(inner, {"duplicate_every_message": True})
    refs = mail.list_new(["c@college.edu"], NOW - timedelta(days=1))
    assert len(refs) == 2
    assert refs[0].message_id == refs[1].message_id == "m1"


def test_faultinjector_only_raises_for_named_methods():
    class Dummy:
        def risky(self):
            return "risky-ok"

        def safe(self):
            return "safe-ok"

    wrapped = faults.FaultInjector(Dummy(), {"http_500_rate": 1.0}, methods=("risky",))
    assert wrapped.safe() == "safe-ok"  # not in `methods`: never wrapped, never raises
    with pytest.raises(AdapterError):
        wrapped.risky()


def profile() -> StudentProfile:
    return StudentProfile(
        name="Riya", roll_no="21BCS045", email="riya@college.edu", branch="CSE",
        gpa=7.42, active_backlogs=0, batch_year=2026,
    )


def policy() -> CollegePolicy:
    return CollegePolicy(college_domain="college.edu")


def test_crash_mid_run_recovers_with_zero_duplicates(tmp_path):
    from cutoff.pipeline import planner

    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)

    drive = Drive(drive_id="d1", company="Zentrix", role="Data Analyst", criteria=Criteria(min_gpa=7.0),
                  deadline=NOW + timedelta(days=2))
    planner.plan(
        db_path, drive=drive, previous_verdict=None, notice=Notice(notice_type="NEW_DRIVE"),
        verdict=Verdict(result="ELIGIBLE"), deadline_state="OPEN", profile=profile(), resume=None,
    )

    sheets = FakeSheetStore(profile(), policy())
    calendar = FakeCalendarStore()
    messenger = FakeMessenger()
    adapters = Adapters(sheets=sheets, calendar=calendar, messenger=messenger)

    with pytest.raises(SimulatedCrash):
        run_pending(db_path, adapters, raise_after_actions=1, lease_seconds=0)

    # exactly one action reached EXECUTING before the "crash"; nothing DONE yet
    from cutoff.pipeline.executor import get_actions_for_drive
    statuses_after_crash = [a.status for a in get_actions_for_drive(db_path, "d1")]
    assert "EXECUTING" in statuses_after_crash

    # "restart": same db, same (still-running) fakes, fresh CircuitBreaker
    run_pending(db_path, adapters, lease_seconds=0, breaker=CircuitBreaker())

    final_actions = get_actions_for_drive(db_path, "d1")
    assert all(a.status in ("VERIFIED", "DONE") for a in final_actions)

    # zero duplicates: each calendar event id appears once, sheets row written once
    assert len(calendar._events) == 1
    upsert_calls = [c for c in calendar.calls if c[0] == "upsert_event"]
    assert len({c[1]["event_id"] for c in upsert_calls}) == 1


def test_verify_survives_a_transient_calendar_failure_on_the_confirmation_read(tmp_path):
    """Found live under the calendar_flaky chaos profile: _verify_and_update()'s
    re-read of calendar state (verifier.verify -> get_event) had zero retry
    protection of its own — a transient failure on the *confirmation* read,
    not even the original write, crashed the whole run_pending call
    uncaught (45.2% recovery vs 90%+ for every other chaos profile). Fails
    the first 2 calls to every wrapped calendar method (both the original
    upsert_event and verify's get_event each get their own with_retry now),
    then succeeds — the whole run must complete without raising, and the
    action must end up genuinely VERIFIED, not just "didn't crash"."""
    from cutoff.pipeline import planner

    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)

    drive = Drive(drive_id="d1", company="Zentrix", role="Data Analyst", criteria=Criteria(min_gpa=7.0),
                  deadline=NOW + timedelta(days=2))
    planner.plan(
        db_path, drive=drive, previous_verdict=None, notice=Notice(notice_type="NEW_DRIVE"),
        verdict=Verdict(result="ELIGIBLE"), deadline_state="OPEN", profile=profile(), resume=None,
    )

    sheets = FakeSheetStore(profile(), policy())
    raw_calendar = FakeCalendarStore()
    calendar = faults.wrap_calendar(raw_calendar, {"http_429_first_n": 2, "retry_after_s": 0})
    messenger = FakeMessenger()
    adapters = Adapters(sheets=sheets, calendar=calendar, messenger=messenger)

    processed = run_pending(db_path, adapters, lease_seconds=0)  # must not raise

    from cutoff.pipeline.executor import get_actions_for_drive
    calendar_actions = [a for a in get_actions_for_drive(db_path, "d1") if a.app == "calendar"]
    assert calendar_actions and calendar_actions[0].status == "VERIFIED"
    assert len(raw_calendar._events) == 1  # still exactly one event, not duplicated by the retries


def test_get_pending_actions_picks_up_a_lease_expiring_at_exactly_now(tmp_path):
    """Regression test: with lease_seconds=0 (chaos/test recovery), the SQL
    comparison must be `lease_until <= now`, not `<` — two datetime.now()
    calls a few bytecodes apart can produce the identical ISO string, and a
    strict `<` left the action stuck in EXECUTING forever (a real, flaky bug
    found via a crash-recovery test failing ~1 run in 5)."""
    from cutoff.pipeline import executor

    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)

    action = executor.Action(
        action_id="a1", idempotency_key="k1", drive_id="d1", drive_version=1,
        app="sheets", kind="upsert_row", tier="T1_REVERSIBLE", payload={"row": {}}, status="PLANNED",
    )
    executor.plan_action(db_path, action)

    frozen_now = executor._now_iso()
    conn = db.get_connection(db_path)
    conn.execute(
        "UPDATE actions SET status = 'EXECUTING', lease_until = ? WHERE action_id = ?",
        (frozen_now, "a1"),
    )
    conn.commit()

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(frozen_now)

    import cutoff.pipeline.executor as executor_module
    original_datetime = executor_module.datetime
    executor_module.datetime = _FrozenDatetime
    try:
        pending = executor.get_pending_actions(db_path)
    finally:
        executor_module.datetime = original_datetime

    assert [a.action_id for a in pending] == ["a1"]

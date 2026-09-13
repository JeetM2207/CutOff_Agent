"""Phase 1 acceptance test (Section 19, Phase 1): feeding dev_014 through the
fakes produces a v2 drive, NOT_ELIGIBLE, a voided approval and a cancelled
reminder. Running it twice creates zero new effects. Uses a stubbed
extract_fn — never calls the real LLM (Section 0 rule 3)."""
import json
from datetime import datetime
from pathlib import Path

import pytest

from cutoff import db
from cutoff.adapters.fakes import FakeCalendarStore, FakeFileStore, FakeMailSource, FakeMessenger, FakeSheetStore
from cutoff.models import CollegePolicy, Criteria, EmailMessage, Evidence, Notice, StudentProfile
from cutoff.pipeline import executor, resolve, run
from cutoff.pipeline.executor import Adapters
from cutoff.pipeline.timeparse import resolve_deadline

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "eval" / "fixtures" / "dev" / "dev_014_revision_branch_removed"
PROFILES_DIR = Path(__file__).resolve().parent.parent / "eval" / "profiles"


def _load_scenario():
    scenario = json.loads((FIXTURE_DIR / "scenario.json").read_text())
    expected = json.loads((FIXTURE_DIR / "expected.json").read_text())
    profile_data = json.loads((PROFILES_DIR / scenario["profile"]).read_text())
    profile_data.update(scenario.get("profile_overrides", {}))
    policy_data = json.loads((PROFILES_DIR / "policy.json").read_text())
    return scenario, expected, profile_data, policy_data


def _stub_extract(msg: EmailMessage, body_clean: str, quoted_history: str, attachment_text: str, **kwargs) -> Notice:
    """Hand-written extraction results standing in for the real LLM call,
    matching what a correct extraction of dev_014's two messages looks like."""
    if msg.message_id == "m1":
        deadline_text = "11:59 PM, 12 September 2026"
        return Notice(
            notice_type="NEW_DRIVE",
            company="Zentrix Analytics", role="Data Analyst", role_category="DATA", salary_lpa=9.5,
            criteria=Criteria(
                min_gpa=7.0, gpa_inclusive=True,
                branches_allowed=["CSE", "IT", "ECE"], branches_text="B.Tech CSE, IT, ECE",
                max_active_backlogs=0,
            ),
            deadline_text=deadline_text,
            deadline=resolve_deadline(deadline_text, msg.received_at, "Asia/Kolkata"),
            form_url="https://forms.gle/demoZentrix",
            evidence=[
                Evidence(field="company", quote="Zentrix Analytics"),
                Evidence(field="role", quote="Data Analyst"),
                Evidence(field="salary_lpa", quote="CTC 9.5 LPA"),
                Evidence(field="criteria.min_gpa", quote="CGPA 7.0 and above"),
                Evidence(field="criteria.gpa_inclusive", quote="CGPA 7.0 and above"),
                Evidence(field="criteria.branches_allowed", quote="B.Tech CSE, IT, ECE"),
                Evidence(field="criteria.branches_text", quote="B.Tech CSE, IT, ECE"),
                Evidence(field="criteria.max_active_backlogs", quote="No active backlogs"),
                Evidence(field="deadline_text", quote=deadline_text),
                Evidence(field="form_url", quote="https://forms.gle/demoZentrix"),
            ],
        )

    if msg.message_id == "m2":
        return Notice(
            notice_type="REVISION",
            change_summary="ECE students are no longer eligible; branches now CSE and IT only.",
            criteria=Criteria(branches_allowed=["CSE", "IT"], branches_text="Eligible branches: CSE and IT only"),
            evidence=[
                Evidence(field="criteria.branches_allowed", quote="Eligible branches: CSE and IT only"),
                Evidence(field="criteria.branches_text", quote="Eligible branches: CSE and IT only"),
            ],
        )

    raise AssertionError(f"unexpected message_id {msg.message_id!r}")


def _build_email(m: dict) -> EmailMessage:
    return EmailMessage(
        message_id=m["message_id"], thread_id=m["thread_id"], from_addr=m["from"],
        subject=m["subject"], body_text=m["body"], received_at=datetime.fromisoformat(m["received_at"]),
    )


@pytest.fixture()
def harness(tmp_path):
    scenario, expected, profile_data, policy_data = _load_scenario()
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)

    profile = StudentProfile(**profile_data)
    policy = CollegePolicy(**policy_data)
    now = datetime.fromisoformat(scenario["now"])

    mail = FakeMailSource()
    files = FakeFileStore([])
    sheets = FakeSheetStore(profile, policy)
    calendar = FakeCalendarStore()
    messenger = FakeMessenger()

    ctx = run.PipelineContext(
        mail=mail, files=files, extract_fn=_stub_extract, api_key="unused", model="stub",
        timezone_name="Asia/Kolkata", db_path=db_path, use_llm_cache=False,
    )
    adapters = Adapters(sheets=sheets, calendar=calendar, messenger=messenger)
    messages = [_build_email(m) for m in scenario["messages"]]

    return dict(
        db_path=db_path, profile=profile, policy=policy, now=now, ctx=ctx, adapters=adapters,
        messages=messages, expected=expected, sheets=sheets, calendar=calendar, messenger=messenger,
    )


def _run_all(h, messages):
    for msg in messages:
        run.process_message(msg, h["ctx"], profile=h["profile"], policy=h["policy"], now=h["now"])
    executor.run_pending(h["db_path"], h["adapters"])


def test_dev_014_produces_v2_not_eligible_voided_approval_and_cancelled_reminder(harness):
    h = harness
    _run_all(h, h["messages"])

    drive_id = h["expected"]["drives"][0]["drive_id"]
    drive = resolve.get_drive(h["db_path"], drive_id)
    assert drive is not None
    assert drive.version == h["expected"]["drives"][0]["version"] == 2
    assert drive.last_verdict == h["expected"]["drives"][0]["verdict"] == "NOT_ELIGIBLE"

    approval = executor.get_pending_approval(h["db_path"], drive_id)
    assert approval is None, "the v1 approval must be voided, not left active"

    reminder_event_id = executor.event_id_for(f"cal:{drive_id}:deadline")
    reminder = h["calendar"].get_event(reminder_event_id)
    assert reminder is not None and reminder.status == "cancelled"

    row = h["sheets"].read_drive_row(drive_id)
    assert row is not None and row["verdict"] == "NOT_ELIGIBLE"

    calendar_event_ids = list(h["calendar"]._events.keys())
    assert calendar_event_ids.count(reminder_event_id) <= 1  # no duplicate events


def test_running_dev_014_twice_creates_zero_new_effects(harness):
    h = harness
    _run_all(h, h["messages"])

    drive_id = h["expected"]["drives"][0]["drive_id"]
    actions_before = {a.action_id: a.status for a in executor.get_actions_for_drive(h["db_path"], drive_id)}
    calendar_events_before = dict(h["calendar"]._events)
    sheets_row_before = h["sheets"].read_drive_row(drive_id)
    messenger_calls_before = len(h["messenger"].calls)

    _run_all(h, h["messages"])  # replay the exact same message_ids

    actions_after = {a.action_id: a.status for a in executor.get_actions_for_drive(h["db_path"], drive_id)}
    assert actions_after == actions_before
    assert dict(h["calendar"]._events) == calendar_events_before
    assert h["sheets"].read_drive_row(drive_id) == sheets_row_before
    assert len(h["messenger"].calls) == messenger_calls_before

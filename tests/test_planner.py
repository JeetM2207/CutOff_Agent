"""Section 11.3's planner table, one scenario per row."""
from datetime import datetime, timedelta, timezone

import pytest

from cutoff import db
from cutoff.models import Criteria, Drive, DriveEvent, Notice, StudentProfile, Verdict
from cutoff.pipeline import executor, planner

NOW = datetime(2026, 9, 10, 20, 0, tzinfo=timezone.utc)
FUTURE_DEADLINE = NOW + timedelta(days=2)
PAST_DEADLINE = NOW - timedelta(hours=1)


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "cutoff.db")
    db.init_db(path)
    return path


def profile() -> StudentProfile:
    return StudentProfile(
        name="Riya Mehta", roll_no="21BCS045", email="riya@college.edu", branch="CSE",
        gpa=7.42, active_backlogs=0, pct_10th=91, pct_12th=86, batch_year=2026,
    )


def make_drive(**overrides) -> Drive:
    base = dict(
        drive_id="zentrix:data-analyst:2026", company="Zentrix", role="Data Analyst",
        version=1, criteria=Criteria(min_gpa=7.0), deadline=FUTURE_DEADLINE,
    )
    base.update(overrides)
    return Drive(**base)


def notice(notice_type="NEW_DRIVE", **overrides) -> Notice:
    base = dict(notice_type=notice_type)
    base.update(overrides)
    return Notice(**base)


def kinds(actions):
    return sorted((a.app, a.kind, a.idempotency_key) for a in actions)


def test_new_drive_eligible_open_sends_approval_and_reminder(db_path):
    drive = make_drive()
    actions = planner.plan(
        db_path, drive=drive, previous_verdict=None, notice=notice(),
        verdict=Verdict(result="ELIGIBLE"), deadline_state="OPEN", profile=profile(), resume=None,
    )
    apps_kinds = {(a.app, a.kind) for a in actions}
    assert ("sheets", "upsert_row") in apps_kinds
    assert ("calendar", "upsert_event") in apps_kinds
    assert ("telegram", "send_msg") in apps_kinds
    tg = next(a for a in actions if a.app == "telegram")
    assert tg.idempotency_key == f"tg:{drive.drive_id}:main"
    button_labels = {b["text"] for b in tg.payload["buttons"]}
    assert button_labels == {"Approve", "Skip", "Remind me in 2h"}
    approval = executor.get_pending_approval(db_path, drive.drive_id)
    assert approval is not None and approval["status"] == "PENDING"


def test_approval_buttons_use_the_stored_approval_id_not_a_truncated_drive_id(db_path):
    # Section 6.5: callback_data must be a short, collision-free opaque token
    # (e.g. "a:7f3k:approve"), not a truncated drive_id slug prefix — two
    # drives could share the same first 8 characters.
    drive = make_drive(drive_id="northwind-systems:software-engineer:2026")
    actions = planner.plan(
        db_path, drive=drive, previous_verdict=None, notice=notice(),
        verdict=Verdict(result="ELIGIBLE"), deadline_state="OPEN", profile=profile(), resume=None,
    )
    tg = next(a for a in actions if a.app == "telegram")
    approval = executor.get_pending_approval(db_path, drive.drive_id)

    tokens_in_buttons = {b["callback_data"].split(":")[1] for b in tg.payload["buttons"]}
    assert tokens_in_buttons == {approval["approval_id"]}
    assert approval["approval_id"] != drive.drive_id[:8]
    assert len(approval["approval_id"]) <= 8


def test_question_buttons_also_use_a_resolvable_token(db_path):
    drive = make_drive(drive_id="northwind-systems:software-engineer:2026")
    verdict = Verdict(result="NEEDS_REVIEW", questions=["Is your GPA exactly 7.0 inclusive?"])
    actions = planner.plan(
        db_path, drive=drive, previous_verdict=None, notice=notice(),
        verdict=verdict, deadline_state="OPEN", profile=profile(), resume=None,
    )
    tg = next(a for a in actions if a.app == "telegram")
    approval = executor.get_pending_approval(db_path, drive.drive_id)

    tokens_in_buttons = {b["callback_data"].split(":")[1] for b in tg.payload["buttons"]}
    assert tokens_in_buttons == {approval["approval_id"]}


def test_new_drive_needs_review_sends_question(db_path):
    drive = make_drive()
    verdict = Verdict(result="NEEDS_REVIEW", questions=["Is your GPA exactly 7.0 inclusive?"])
    actions = planner.plan(
        db_path, drive=drive, previous_verdict=None, notice=notice(),
        verdict=verdict, deadline_state="OPEN", profile=profile(), resume=None,
    )
    tg = next(a for a in actions if a.app == "telegram")
    assert "eligible" in tg.payload["text"].lower() or "call" in tg.payload["text"].lower()
    button_labels = {b["text"] for b in tg.payload["buttons"]}
    assert button_labels == {"Yes, I'm eligible", "No", "Show email"}


def test_new_drive_not_eligible_has_no_telegram_message(db_path):
    drive = make_drive()
    actions = planner.plan(
        db_path, drive=drive, previous_verdict=None, notice=notice(),
        verdict=Verdict(result="NOT_ELIGIBLE", reasons=["GPA"]), deadline_state="OPEN",
        profile=profile(), resume=None,
    )
    assert all(a.app != "telegram" for a in actions)
    assert any(a.app == "sheets" for a in actions)


def test_revision_eligible_to_not_eligible_voids_and_cancels(db_path):
    drive = make_drive(version=1)
    # seed a pending approval + deadline reminder as if v1 had been ELIGIBLE
    planner.plan(
        db_path, drive=drive, previous_verdict=None, notice=notice(),
        verdict=Verdict(result="ELIGIBLE"), deadline_state="OPEN", profile=profile(), resume=None,
    )

    drive_v2 = make_drive(version=2, criteria=Criteria(min_gpa=7.0, branches_allowed=["ECE"]))
    actions = planner.plan(
        db_path, drive=drive_v2, previous_verdict="ELIGIBLE",
        notice=notice("REVISION", change_summary="branch removed"),
        verdict=Verdict(result="NOT_ELIGIBLE", reasons=["BRANCH"]), deadline_state="OPEN",
        profile=profile(), resume=None,
    )

    cancel_action = next(a for a in actions if a.kind == "cancel_event")
    assert cancel_action.idempotency_key == f"cal:{drive.drive_id}:deadline:cancel"
    edit_action = next(a for a in actions if a.app == "telegram")
    assert "no longer eligible" in edit_action.payload["text"].lower()

    approval = executor.get_pending_approval(db_path, drive.drive_id)
    assert approval is None  # voided


def test_revision_not_eligible_to_eligible_sends_new_versioned_approval(db_path):
    drive_v2 = make_drive(version=2)
    actions = planner.plan(
        db_path, drive=drive_v2, previous_verdict="NOT_ELIGIBLE",
        notice=notice("REVISION", change_summary="branch added back"),
        verdict=Verdict(result="ELIGIBLE"), deadline_state="OPEN", profile=profile(), resume=None,
    )
    tg = next(a for a in actions if a.app == "telegram")
    assert tg.idempotency_key == f"tg:{drive_v2.drive_id}:v2"
    assert tg.idempotency_key != f"tg:{drive_v2.drive_id}:main"


def test_cancellation_compensates_everything(db_path):
    drive = make_drive(
        events=[DriveEvent(kind="TEST", start=FUTURE_DEADLINE)],
    )
    planner.plan(
        db_path, drive=drive, previous_verdict=None, notice=notice(),
        verdict=Verdict(result="ELIGIBLE"), deadline_state="OPEN", profile=profile(), resume=None,
    )

    cancelled_drive = drive.model_copy(update={"version": 2, "status": "CANCELLED"})
    actions = planner.plan(
        db_path, drive=cancelled_drive, previous_verdict="ELIGIBLE",
        notice=notice("CANCELLATION", change_summary="drive cancelled"),
        verdict=Verdict(result="NOT_ELIGIBLE"), deadline_state="OPEN", profile=profile(), resume=None,
    )

    kinds_set = {(a.app, a.kind) for a in actions}
    assert ("calendar", "cancel_event") in kinds_set
    assert ("telegram", "send_msg") in kinds_set
    assert ("sheets", "upsert_row") in kinds_set
    edit = next(a for a in actions if a.app == "telegram")
    assert "cancelled" in edit.payload["text"].lower()
    approval = executor.get_pending_approval(db_path, drive.drive_id)
    assert approval is None


def test_reminder_replanning_same_action_is_idempotent(db_path):
    drive = make_drive()
    first = planner.plan(
        db_path, drive=drive, previous_verdict=None, notice=notice(),
        verdict=Verdict(result="ELIGIBLE"), deadline_state="OPEN", profile=profile(), resume=None,
    )
    before_ids = {a.action_id for a in executor.get_actions_for_drive(db_path, drive.drive_id)}

    second = planner.plan(
        db_path, drive=drive, previous_verdict="ELIGIBLE", notice=notice("REVISION"),
        verdict=Verdict(result="ELIGIBLE"), deadline_state="OPEN", profile=profile(), resume=None,
    )
    after_ids = {a.action_id for a in executor.get_actions_for_drive(db_path, drive.drive_id)}

    # same idempotency keys -> same rows updated in place, not duplicated
    assert before_ids == after_ids


def test_deadline_passed_new_drive_sends_fyi_only(db_path):
    drive = make_drive(deadline=PAST_DEADLINE)
    actions = planner.plan(
        db_path, drive=drive, previous_verdict=None, notice=notice(),
        verdict=Verdict(result="ELIGIBLE"), deadline_state="PASSED", profile=profile(), resume=None,
    )
    assert not any(a.kind == "upsert_event" for a in actions if a.app == "calendar")
    tg = next(a for a in actions if a.app == "telegram")
    assert "missed" in tg.payload["text"].lower()


def test_schedule_event_with_clash_warning_embeds_it_and_alerts(db_path):
    ev = DriveEvent(kind="TEST", start=NOW + timedelta(days=1), end=NOW + timedelta(days=1, hours=2))
    drive = make_drive(events=[ev], version=2)
    actions = planner.plan(
        db_path, drive=drive, previous_verdict="ELIGIBLE", notice=notice("SCHEDULE"),
        verdict=Verdict(result="ELIGIBLE"), deadline_state="OPEN", profile=profile(), resume=None,
        event_clash_warnings=["Clashes with your exam schedule: DBMS Midterm (2026-09-11T10:00:00+00:00)."],
    )
    cal_action = next(a for a in actions if a.app == "calendar")
    assert "DBMS Midterm" in cal_action.payload["event"]["description"]
    tg_action = next(a for a in actions if a.app == "telegram")
    assert "Heads up" in tg_action.payload["text"]
    assert "DBMS Midterm" in tg_action.payload["text"]


def test_schedule_event_without_clash_has_no_alert(db_path):
    ev = DriveEvent(kind="TEST", start=NOW + timedelta(days=1), end=NOW + timedelta(days=1, hours=2))
    drive = make_drive(events=[ev], version=2)
    actions = planner.plan(
        db_path, drive=drive, previous_verdict="ELIGIBLE", notice=notice("SCHEDULE"),
        verdict=Verdict(result="ELIGIBLE"), deadline_state="OPEN", profile=profile(), resume=None,
        event_clash_warnings=[None],
    )
    assert all(a.app != "telegram" for a in actions)
    cal_action = next(a for a in actions if a.app == "calendar")
    assert cal_action.payload["event"]["description"] is None


class _FakeShortlistResult:
    def __init__(self, status, page_number=None, evidence_line=None):
        self.status = status
        self.page_number = page_number
        self.evidence_line = evidence_line


def test_shortlist_match_reports_shortlisted_with_evidence(db_path):
    drive = make_drive()
    result = _FakeShortlistResult("SHORTLISTED", page_number=4, evidence_line="21BCS045 RIYA MEHTA")
    actions = planner.plan(
        db_path, drive=drive, previous_verdict="ELIGIBLE", notice=notice("SHORTLIST"),
        verdict=Verdict(result="ELIGIBLE"), deadline_state="OPEN", profile=profile(), resume=None,
        shortlist_result=result,
    )
    row_action = next(a for a in actions if a.app == "sheets")
    assert row_action.payload["row"]["status"] == "SHORTLISTED"
    tg_action = next(a for a in actions if a.app == "telegram")
    assert "page 4" in tg_action.payload["text"]
    assert "21BCS045 RIYA MEHTA" in tg_action.payload["text"]


def test_shortlist_name_only_match_never_says_shortlisted(db_path):
    drive = make_drive()
    result = _FakeShortlistResult("NAME_ONLY_MATCH", page_number=7, evidence_line="21BCS054 RIYA MEHTA")
    actions = planner.plan(
        db_path, drive=drive, previous_verdict="ELIGIBLE", notice=notice("SHORTLIST"),
        verdict=Verdict(result="ELIGIBLE"), deadline_state="OPEN", profile=profile(), resume=None,
        shortlist_result=result,
    )
    row_action = next(a for a in actions if a.app == "sheets")
    assert row_action.payload["row"]["status"] == "SHORTLIST_REVIEW"
    tg_action = next(a for a in actions if a.app == "telegram")
    assert "shortlisted" not in tg_action.payload["text"].lower()
    assert "different roll number" in tg_action.payload["text"] or "doesn't match" in tg_action.payload["text"]


def test_shortlist_not_listed_updates_row_quietly(db_path):
    drive = make_drive()
    result = _FakeShortlistResult("NOT_LISTED")
    actions = planner.plan(
        db_path, drive=drive, previous_verdict="ELIGIBLE", notice=notice("SHORTLIST"),
        verdict=Verdict(result="ELIGIBLE"), deadline_state="OPEN", profile=profile(), resume=None,
        shortlist_result=result,
    )
    assert all(a.app != "telegram" for a in actions)
    row_action = next(a for a in actions if a.app == "sheets")
    assert row_action.payload["row"]["status"] == "NOT_SHORTLISTED"


# --- Phase 4 fix: plan_suspicious's `kind` must keep "Watch out:" (and the
# scam-recall grading it drives) exclusive to genuine scam/injection signals
# — an extraction hiccup or an ambiguous drive match is not a scam.

def test_plan_suspicious_default_kind_is_a_scam_warning(db_path):
    action = planner.plan_suspicious(
        db_path, key="tg:suspicious:m1", drive=None, company_hint="Fake Corp",
        signals=["FEE_REQUEST"],
    )
    assert action.payload["text"].startswith("Watch out:")
    assert action.payload["buttons"] == [{"text": "Show why", "callback_data": "s:show"}]


def test_plan_suspicious_unreadable_kind_is_not_a_scam_warning(db_path):
    action = planner.plan_suspicious(
        db_path, key="tg:unreadable:m1", drive=None, company_hint="Some Drive",
        signals=["I couldn't read this email reliably."], kind="unreadable",
    )
    assert not action.payload["text"].startswith("Watch out:")
    assert "couldn't read" in action.payload["text"].lower()
    assert action.payload["buttons"] is None


def test_plan_suspicious_unresolved_kind_is_not_a_scam_warning(db_path):
    action = planner.plan_suspicious(
        db_path, key="tg:unresolved:m1", drive=None, company_hint="Some Drive",
        signals=["Is this about Drive A or Drive B?"], kind="unresolved",
    )
    assert not action.payload["text"].startswith("Watch out:")
    assert "Your call:" in action.payload["text"]
    assert action.payload["buttons"] is None


def test_format_deadline_is_human_readable_in_ist_regardless_of_stored_offset():
    # Found live: raw ISO timestamps ("2026-09-22T18:00:00+05:30") in
    # Telegram/Calendar text read as confusing. Always shown in Asia/Kolkata,
    # 12-hour clock, no leading zero on the hour — and never the Sheet row,
    # which stays ISO for verifier.verify()'s exact-match comparison.
    assert planner._format_deadline(None) == "not stated"
    utc_deadline = datetime(2026, 9, 22, 12, 30, tzinfo=timezone.utc)  # 18:00 IST
    assert planner._format_deadline(utc_deadline) == "Tue, 22 Sep 2026, 6:00 PM IST"


def test_answer_card_and_approval_text_never_contain_raw_iso_deadline(db_path):
    drive = make_drive(deadline=datetime(2026, 9, 22, 12, 30, tzinfo=timezone.utc))
    actions = planner.plan(
        db_path, drive=drive, previous_verdict=None, notice=notice(),
        verdict=Verdict(result="ELIGIBLE"), deadline_state="OPEN", profile=profile(), resume=None,
    )
    approval = next(a for a in actions if a.app == "telegram")
    assert "2026-09-22T12:30:00" not in approval.payload["text"]
    assert "6:00 PM IST" in approval.payload["text"]

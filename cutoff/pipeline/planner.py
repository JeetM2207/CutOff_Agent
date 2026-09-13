"""Planner (Section 11.3): turns (drive, previous verdict, verdict, notice type)
into a list of Actions with idempotency keys, and persists them via the ledger.
Never executes anything itself — `executor.run_pending` does that."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from cutoff.models import Action, CalendarEvent, Drive, Notice, ResumeFile, StudentProfile, Verdict
from cutoff.pipeline import executor
from cutoff.pipeline.executor import event_id_for  # noqa: F401  (re-exported for callers/tests)
from cutoff.pipeline.resume import GENERATED_RESUME_NAME

DEADLINE_REMINDER_LEAD = timedelta(hours=3)

_DISPLAY_TZ = ZoneInfo("Asia/Kolkata")


def _format_deadline(deadline: datetime | None) -> str:
    """Human-facing deadline text for Telegram/Calendar (never the Sheet row,
    which stays ISO for verifier.verify()'s exact-match comparison). Always
    shown in Asia/Kolkata regardless of the stored offset — this project's
    students are always reading it as Indian campus recruiting deadlines."""
    if deadline is None:
        return "not stated"
    local = deadline.astimezone(_DISPLAY_TZ)
    hour12 = local.strftime("%I").lstrip("0") or "12"
    return local.strftime(f"%a, %d %b %Y, {hour12}:%M %p IST")


def _sheets_row(drive: Drive, verdict: Verdict | None) -> dict:
    return {
        "drive_id": drive.drive_id,
        "company": drive.company,
        "role": drive.role,
        "version": drive.version,
        "verdict": verdict.result if verdict else None,
        "reasons": ",".join(verdict.reasons) if verdict else "",
        "deadline": drive.deadline.isoformat() if drive.deadline else None,
        "status": drive.status if drive.status != "OPEN" else ("REGISTERED" if drive.registered else "OPEN"),
    }


def _new_action(*, idempotency_key: str, drive: Drive, app: str, kind: str, tier: str, payload: dict) -> Action:
    return Action(
        action_id=uuid.uuid4().hex, idempotency_key=idempotency_key, drive_id=drive.drive_id,
        drive_version=drive.version, app=app, kind=kind, tier=tier, payload=payload,
    )


def _plan_sheets_row(db_path: str, drive: Drive, verdict: Verdict | None) -> Action:
    action = _new_action(
        idempotency_key=f"sheets:{drive.drive_id}:row", drive=drive, app="sheets", kind="upsert_row",
        tier="T1_REVERSIBLE", payload={"row": _sheets_row(drive, verdict)},
    )
    return executor.plan_action(db_path, action)


def _plan_deadline_reminder(db_path: str, drive: Drive) -> Action | None:
    if drive.deadline is None:
        return None
    start = drive.deadline - DEADLINE_REMINDER_LEAD
    event = CalendarEvent(
        event_id="unset", title=f"{drive.company} — {drive.role} deadline",
        start=start, end=start + timedelta(minutes=30),
        description=f"Registration deadline for {drive.company} ({drive.role}) is {_format_deadline(drive.deadline)}.",
    )
    action = _new_action(
        idempotency_key=f"cal:{drive.drive_id}:deadline", drive=drive, app="calendar", kind="upsert_event",
        tier="T1_REVERSIBLE", payload={"event": event.model_dump(mode="json")},
    )
    return executor.plan_action(db_path, action)


def _plan_cancel_deadline_reminder(db_path: str, drive: Drive) -> Action:
    real_event_id = event_id_for(f"cal:{drive.drive_id}:deadline")
    action = _new_action(
        idempotency_key=f"cal:{drive.drive_id}:deadline:cancel", drive=drive, app="calendar",
        kind="cancel_event", tier="T1_REVERSIBLE", payload={"event_id": real_event_id},
    )
    return executor.plan_action(db_path, action)


def _plan_drive_events(db_path: str, drive: Drive, clash_warnings: list[str | None] | None = None) -> list[Action]:
    actions = []
    clash_warnings = clash_warnings or []
    hit_warnings: list[str] = []
    for i, ev in enumerate(drive.events):
        warning = clash_warnings[i] if i < len(clash_warnings) else None
        if warning:
            hit_warnings.append(warning)
        event = CalendarEvent(
            event_id="unset", title=f"{drive.company} — {ev.kind.title()}",
            start=ev.start, end=ev.end, location=ev.location_or_link, description=warning,
        )
        action = _new_action(
            idempotency_key=f"cal:{drive.drive_id}:event:{ev.kind}:{i}", drive=drive, app="calendar",
            kind="upsert_event", tier="T1_REVERSIBLE", payload={"event": event.model_dump(mode="json")},
        )
        actions.append(executor.plan_action(db_path, action))

    if hit_warnings:
        action = _new_action(
            idempotency_key=f"tg:{drive.drive_id}:clash:v{drive.version}", drive=drive, app="telegram",
            kind="send_msg", tier="T1_REVERSIBLE",
            payload={"text": f"Heads up, {drive.company} ({drive.role}): " + " ".join(hit_warnings), "buttons": None},
        )
        actions.append(executor.plan_action(db_path, action))

    return actions


def _answer_card_text(
    drive: Drive, profile: StudentProfile, resume: ResumeFile | None, resume_reason: str | None = None,
    prefilled_form_url: str | None = None,
) -> str:
    resume_line = f"Resume: {resume.web_view_link}" if resume else "Resume: none on file, check manually"
    if resume and resume_reason:
        if resume.name == GENERATED_RESUME_NAME:
            resume_line += f"\nGenerated bespoke resume — tailored for: {resume_reason}"
        else:
            resume_line += f"\nWhy this one: {resume_reason}"
    deadline_line = f"Deadline: {_format_deadline(drive.deadline)}"
    if prefilled_form_url:
        # Section 6.5's guarantee is unchanged: this is a convenience link
        # with the student's own profile data already filled in — they still
        # have to open it and click Submit themselves.
        form_line = f"Form (pre-filled, just review & submit): {prefilled_form_url}"
    elif drive.form_url:
        form_line = f"Form: {drive.form_url}"
    else:
        form_line = "Form: not found, check the email"
    return (
        f"{drive.company} — {drive.role}\n"
        f"{profile.name} ({profile.roll_no}, {profile.branch})\n"
        f"GPA: {profile.gpa}\n"
        f"{deadline_line}\n"
        f"{resume_line}\n"
        f"{form_line}"
    )


def _plan_approval(
    db_path: str, drive: Drive, profile: StudentProfile, resume: ResumeFile | None, *,
    new_epoch: bool, resume_reason: str | None = None, prefilled_form_url: str | None = None,
) -> Action:
    key = f"tg:{drive.drive_id}:v{drive.version}" if new_epoch else f"tg:{drive.drive_id}:main"
    # Section 6.5: callback_data is capped at 64 bytes and drive_ids are
    # unbounded slugs, so button tokens must be short AND collision-free —
    # a truncated drive_id is neither. Generate the token first, so the same
    # id can be handed to both the buttons and the `approvals` row.
    token = executor.new_short_token()
    text = (
        f"Register? You're eligible for {drive.company} ({drive.role}). "
        f"Deadline {_format_deadline(drive.deadline)}.\n\n"
        + _answer_card_text(drive, profile, resume, resume_reason, prefilled_form_url)
    )
    buttons = [
        {"text": "Approve", "callback_data": f"a:{token}:approve"},
        {"text": "Skip", "callback_data": f"a:{token}:skip"},
        {"text": "Remind me in 2h", "callback_data": f"a:{token}:snooze"},
    ]
    action = _new_action(
        idempotency_key=key, drive=drive, app="telegram", kind="send_msg", tier="T1_REVERSIBLE",
        payload={"text": text, "buttons": buttons},
    )
    planned = executor.plan_action(db_path, action)
    executor.create_approval(db_path, planned.action_id, drive.drive_id, drive.version, approval_id=token)
    return planned


def _plan_question(db_path: str, drive: Drive, verdict: Verdict, *, new_epoch: bool) -> Action:
    key = f"tg:{drive.drive_id}:v{drive.version}" if new_epoch else f"tg:{drive.drive_id}:main"
    # A question isn't an "approval," but it needs the same short, collision-
    # free, bot-resolvable token — reuses the `approvals` table as a generic
    # "token -> drive" lookup rather than inventing a second table for it.
    token = executor.new_short_token()
    question = " ".join(verdict.questions) if verdict.questions else "Are you eligible for this drive?"
    text = f"Your call: {drive.company} ({drive.role}). {question}"
    buttons = [
        {"text": "Yes, I'm eligible", "callback_data": f"q:{token}:yes"},
        {"text": "No", "callback_data": f"q:{token}:no"},
        {"text": "Show email", "callback_data": f"q:{token}:show"},
    ]
    action = _new_action(
        idempotency_key=key, drive=drive, app="telegram", kind="send_msg", tier="T1_REVERSIBLE",
        payload={"text": text, "buttons": buttons},
    )
    planned = executor.plan_action(db_path, action)
    executor.create_approval(db_path, planned.action_id, drive.drive_id, drive.version, approval_id=token)
    return planned


_SHORTLIST_STATUS_MAP = {
    "SHORTLISTED": "SHORTLISTED",
    "NAME_ONLY_MATCH": "SHORTLIST_REVIEW",
    "NOT_LISTED": "NOT_SHORTLISTED",
    "UNREADABLE": "SHORTLIST_UNREADABLE",
}


def _plan_shortlist_message(db_path: str, drive: Drive, shortlist_result) -> Action | None:
    """Section 13's result table. Never reports 'shortlisted' on a name-only
    match — that's exactly the false_shortlisted=0 target."""
    key = f"tg:{drive.drive_id}:shortlist:v{drive.version}"
    if shortlist_result.status == "SHORTLISTED":
        text = (
            f"You're on the {drive.company} shortlist "
            f"(page {shortlist_result.page_number}: '{shortlist_result.evidence_line}')."
        )
        buttons = None
    elif shortlist_result.status == "NAME_ONLY_MATCH":
        text = (
            f"Your name appears on page {shortlist_result.page_number} of the {drive.company} shortlist "
            f"('{shortlist_result.evidence_line}') but the roll number doesn't match yours. "
            "It might be someone else with the same name."
        )
        buttons = [{"text": "Show why", "callback_data": "s:show"}]
    elif shortlist_result.status == "UNREADABLE":
        text = f"I can't read the {drive.company} shortlist PDF (it looks like a scanned image). Check it manually."
        buttons = None
    else:  # NOT_LISTED: update the row quietly, no message (Section 13)
        return None

    action = _new_action(
        idempotency_key=key, drive=drive, app="telegram", kind="send_msg", tier="T1_REVERSIBLE",
        payload={"text": text, "buttons": buttons},
    )
    return executor.plan_action(db_path, action)


def _plan_edit_main(db_path: str, drive: Drive, text: str, buttons: list[dict] | None = None) -> Action:
    action = _new_action(
        idempotency_key=f"tg:{drive.drive_id}:main", drive=drive, app="telegram", kind="send_msg",
        tier="T1_REVERSIBLE", payload={"text": text, "buttons": buttons},
    )
    return executor.plan_action(db_path, action)


def plan(
    db_path: str,
    *,
    drive: Drive,
    previous_verdict: str | None,
    notice: Notice,
    verdict: Verdict,
    deadline_state: str,
    profile: StudentProfile,
    resume: ResumeFile | None,
    resume_reason: str | None = None,
    prefilled_form_url: str | None = None,
    event_clash_warnings: list[str | None] | None = None,
    shortlist_result=None,
) -> list[Action]:
    """Section 11.3's planner table. Returns every action planned (some
    branches plan several)."""
    actions: list[Action] = []
    notice_type = notice.notice_type

    if notice_type == "CANCELLATION":
        for i, _ in enumerate(drive.events):
            actions.append(executor.plan_action(db_path, _new_action(
                idempotency_key=f"cal:{drive.drive_id}:event:{drive.events[i].kind}:{i}:cancel",
                drive=drive, app="calendar", kind="cancel_event", tier="T1_REVERSIBLE",
                payload={"event_id": event_id_for(f"cal:{drive.drive_id}:event:{drive.events[i].kind}:{i}")},
            )))
        actions.append(_plan_cancel_deadline_reminder(db_path, drive))
        executor.void_pending_approvals(db_path, drive.drive_id)
        actions.append(_plan_edit_main(db_path, drive, f"{drive.company} ({drive.role}): drive cancelled."))
        actions.append(_plan_sheets_row(db_path, drive, verdict))
        return actions

    if notice_type == "NON_DRIVE":
        return actions

    if notice_type == "SCHEDULE":
        actions.append(_plan_sheets_row(db_path, drive, verdict))
        actions.extend(_plan_drive_events(db_path, drive, event_clash_warnings))
        return actions

    if notice_type == "SHORTLIST":
        row = _sheets_row(drive, verdict)
        if shortlist_result is not None:
            row["status"] = _SHORTLIST_STATUS_MAP.get(shortlist_result.status, row["status"])
        actions.append(executor.plan_action(db_path, _new_action(
            idempotency_key=f"sheets:{drive.drive_id}:row", drive=drive, app="sheets", kind="upsert_row",
            tier="T1_REVERSIBLE", payload={"row": row},
        )))
        if shortlist_result is not None:
            msg_action = _plan_shortlist_message(db_path, drive, shortlist_result)
            if msg_action is not None:
                actions.append(msg_action)
        return actions

    if notice_type == "REMINDER":
        # run.py only calls plan() when something meaningful changed; a true
        # no-op reminder never reaches here.
        actions.append(_plan_sheets_row(db_path, drive, verdict))
        return actions

    # NEW_DRIVE / REVISION share the eligibility-transition table.
    became_ineligible = previous_verdict == "ELIGIBLE" and verdict.result == "NOT_ELIGIBLE"
    became_eligible = previous_verdict == "NOT_ELIGIBLE" and verdict.result == "ELIGIBLE"

    actions.append(_plan_sheets_row(db_path, drive, verdict))

    if verdict.result == "NOT_ELIGIBLE":
        if became_ineligible:
            executor.void_pending_approvals(db_path, drive.drive_id)
            actions.append(_plan_cancel_deadline_reminder(db_path, drive))
            if drive.registered:
                actions.append(_plan_edit_main(
                    db_path, drive,
                    f"You registered for {drive.company} ({drive.role}), but the rules changed and you're "
                    f"no longer eligible: {', '.join(verdict.reasons)}. Contact the career office.",
                ))
            else:
                actions.append(_plan_edit_main(
                    db_path, drive,
                    f"No longer eligible for {drive.company} ({drive.role}): {', '.join(verdict.reasons)}. "
                    "Don't register.",
                ))
        # else: not eligible from the start — tracker row only, no message.
        return actions

    if deadline_state == "PASSED":
        if previous_verdict is None:
            actions.append(_plan_edit_main(
                db_path, drive,
                f"You missed this one: {drive.company} ({drive.role}) registration closed at "
                f"{_format_deadline(drive.deadline) if drive.deadline else 'the stated deadline'}.",
            ))
        return actions

    if verdict.result == "ELIGIBLE":
        actions.append(_plan_deadline_reminder(db_path, drive))
        if became_eligible:
            actions.append(_plan_approval(
                db_path, drive, profile, resume, new_epoch=True,
                resume_reason=resume_reason, prefilled_form_url=prefilled_form_url,
            ))
        elif previous_verdict in (None, "ELIGIBLE"):
            if previous_verdict is None:
                actions.append(_plan_approval(
                    db_path, drive, profile, resume, new_epoch=False,
                    resume_reason=resume_reason, prefilled_form_url=prefilled_form_url,
                ))
            else:
                actions.append(_plan_edit_main(
                    db_path, drive,
                    f"Updated: {notice.change_summary or 'the drive changed'}.\n\n"
                    + _answer_card_text(drive, profile, resume, resume_reason, prefilled_form_url),
                ))
        return actions

    if verdict.result == "NEEDS_REVIEW":
        actions.append(_plan_deadline_reminder(db_path, drive))
        if previous_verdict is None:
            actions.append(_plan_question(db_path, drive, verdict, new_epoch=False))
        else:
            actions.append(_plan_edit_main(
                db_path, drive,
                f"Updated: {notice.change_summary or 'the drive changed'}. "
                + (" ".join(verdict.questions) if verdict.questions else ""),
            ))
        return actions

    return actions


def plan_suspicious(db_path: str, *, key: str, drive: Drive | None, company_hint: str, signals: list[str],
                     kind: str = "suspicious") -> Action:
    """A generic "send a Telegram heads-up, no other action" message for the
    three run.py cases that don't fit the normal (drive, verdict) planning
    path. `kind` picks the wording and buttons — genuinely important so a
    parsing hiccup or an ambiguous drive match never reads as a scam warning
    to the student (and so `scam_false_alarms` grading can't confuse the two
    — Section 0 rule 1: found in Phase 2's baseline, fixed here in Phase 4)."""
    if kind == "unreadable":
        text = f"I couldn't read an email about \"{company_hint}\" reliably. Check your inbox manually."
        buttons = None
    elif kind == "unresolved":
        question = signals[0] if signals else f"I need your input on a message about \"{company_hint}\"."
        text = f"Your call: {question}"
        buttons = None
    else:  # "suspicious": a real scam/lookalike/injection signal
        text = f"Watch out: a message about \"{company_hint}\" looks suspicious ({', '.join(signals)}). No action taken."
        buttons = [{"text": "Show why", "callback_data": "s:show"}]

    action = _new_action(
        idempotency_key=key,
        drive=drive or Drive(drive_id="unknown", company=company_hint, role="unknown"),
        app="telegram", kind="send_msg", tier="T1_REVERSIBLE",
        payload={"text": text, "buttons": buttons},
    )
    return executor.plan_action(db_path, action)

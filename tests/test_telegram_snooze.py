""""Remind me in 2h" (Section 6.5): tapping it used to just say "OK" and do
nothing — the approval stayed silently un-resolved forever, with no actual
follow-up message ever sent (found live, self-disclosed in a code comment).
Now it takes the approval out of PENDING, records when to resurface it, and
TelegramBotLoop.resend_due_reminders() (called once per bot-loop tick, same
cadence as everything else there) resends the original card under a fresh
token once the 2 hours are up."""
from datetime import datetime, timedelta, timezone

from cutoff import db
from cutoff.adapters.fakes import FakeCalendarStore, FakeMessenger, FakeSheetStore
from cutoff.bot.telegram_loop import TelegramBotLoop
from cutoff.models import Criteria, Drive
from cutoff.pipeline import executor, resolve
from cutoff.pipeline.executor import Action, Adapters

NOW = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)


def _drive(**overrides) -> Drive:
    base = dict(
        drive_id="zentrix:data-analyst:2026", company="Zentrix", role="Data Analyst",
        version=1, criteria=Criteria(min_gpa=7.0), last_verdict="ELIGIBLE",
    )
    base.update(overrides)
    return Drive(**base)


def _bot(db_path) -> tuple[TelegramBotLoop, Adapters]:
    adapters = Adapters(sheets=FakeSheetStore(None, None), calendar=FakeCalendarStore(), messenger=FakeMessenger())
    return TelegramBotLoop("token", "chat1", db_path, adapters), adapters


def _plan_original_approval(db_path, drive, adapters) -> str:
    """Mimics planner._plan_approval + the worker loop's own run_pending:
    an Approve/Skip/Remind card with a token, planned, executed (so it's
    already "sent" before the test's own resend_due_reminders call), and
    recorded as a PENDING approval."""
    token = executor.new_short_token()
    action = Action(
        action_id="a1", idempotency_key=f"tg:{drive.drive_id}:main", drive_id=drive.drive_id,
        drive_version=drive.version, app="telegram", kind="send_msg", tier="T1_REVERSIBLE",
        payload={
            "text": f"Register? You're eligible for {drive.company} ({drive.role}).",
            "buttons": [
                {"text": "Approve", "callback_data": f"a:{token}:approve"},
                {"text": "Skip", "callback_data": f"a:{token}:skip"},
                {"text": "Remind me in 2h", "callback_data": f"a:{token}:snooze"},
            ],
        },
    )
    planned = executor.plan_action(db_path, action)
    executor.run_pending(db_path, adapters)
    executor.create_approval(db_path, action_id=planned.action_id, drive_id=drive.drive_id,
                              drive_version=drive.version, approval_id=token)
    return token


def test_snooze_approval_moves_out_of_pending_and_records_when_due(tmp_path):
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)
    drive = _drive()
    resolve.save_drive(db_path, drive)
    bot, adapters = _bot(db_path)
    token = _plan_original_approval(db_path, drive, adapters)

    executor.snooze_approval(db_path, token, NOW)

    assert executor.get_pending_approval(db_path, drive.drive_id) is None  # no longer "pending"
    assert executor.get_due_snoozed_approvals(db_path, NOW) == []  # not due yet
    assert len(executor.get_due_snoozed_approvals(db_path, NOW + timedelta(hours=2, minutes=1))) == 1


def test_resend_due_reminders_resends_under_a_fresh_token(tmp_path):
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)
    drive = _drive()
    resolve.save_drive(db_path, drive)
    bot, adapters = _bot(db_path)
    old_token = _plan_original_approval(db_path, drive, adapters)
    executor.snooze_approval(db_path, old_token, NOW)
    adapters.messenger.calls.clear()  # the original card was already "sent" above

    resent = bot.resend_due_reminders(now=NOW + timedelta(hours=2, minutes=1))

    assert resent == 1
    send_calls = [c for c in adapters.messenger.calls if c[0] == "send"]
    assert len(send_calls) == 1
    new_buttons = send_calls[0][1]["buttons"]
    new_token = new_buttons[0].callback_data.split(":")[1]
    assert new_token != old_token
    assert all(b.callback_data == f"a:{new_token}:{b.callback_data.split(':')[2]}" for b in new_buttons)

    # A fresh PENDING approval now exists under the new token; the old one is done.
    assert executor.get_pending_approval(db_path, drive.drive_id)["approval_id"] == new_token
    conn = db.get_connection(db_path)
    old_row = conn.execute("SELECT status FROM approvals WHERE approval_id = ?", (old_token,)).fetchone()
    assert old_row["status"] == "RESENT"


def test_resend_due_reminders_leaves_not_yet_due_snoozes_alone(tmp_path):
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)
    drive = _drive()
    resolve.save_drive(db_path, drive)
    bot, adapters = _bot(db_path)
    token = _plan_original_approval(db_path, drive, adapters)
    executor.snooze_approval(db_path, token, NOW)  # due in 2h, not now
    adapters.messenger.calls.clear()

    resent = bot.resend_due_reminders(now=NOW + timedelta(minutes=1))  # nowhere near 2h yet

    assert resent == 0
    assert adapters.messenger.calls == []


def test_resend_due_reminders_voids_a_stale_snooze_instead_of_resending_outdated_info(tmp_path):
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)
    drive = _drive()
    resolve.save_drive(db_path, drive)
    bot, adapters = _bot(db_path)
    old_token = _plan_original_approval(db_path, drive, adapters)
    executor.snooze_approval(db_path, old_token, NOW)
    adapters.messenger.calls.clear()

    # A correction landed while snoozed: the drive moved to v2.
    drive.version = 2
    resolve.save_drive(db_path, drive)

    resent = bot.resend_due_reminders(now=NOW + timedelta(hours=2, minutes=1))

    assert resent == 0
    assert adapters.messenger.calls == []
    conn = db.get_connection(db_path)
    row = conn.execute("SELECT status FROM approvals WHERE approval_id = ?", (old_token,)).fetchone()
    assert row["status"] == "VOIDED"

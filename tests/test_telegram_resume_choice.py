"""Section 6.4 extension: the "Use my resume / Generate tailored resume"
choice card and its resolution. Complements test_pipeline_resume_match.py's
full process_message-through-generate integration test with the narrower
telegram_loop.py-level cases: the static "use" path, graceful degradation
when nothing is wired up, double-tap safety, and the resend-token fix for a
card with mixed "r:"/"a:" callback kinds. Never calls a real LLM or network
(Section 0 rule 3)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from cutoff import db
from cutoff.adapters.fakes import FakeCalendarStore, FakeFileStore, FakeMessenger, FakeSheetStore
from cutoff.bot.telegram_loop import TelegramBotLoop
from cutoff.models import Criteria, Drive, MasterProfile, MasterProfileProject, ResumeFile, StudentProfile
from cutoff.pipeline import executor, planner, resolve
from cutoff.pipeline.executor import Adapters

NOW = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)
PROFILE = StudentProfile(
    name="Riya Mehta", roll_no="21BCS045", email="riya@college.edu", branch="CSE",
    gpa=7.42, active_backlogs=0, batch_year=2026,
)


def _drive(**overrides) -> Drive:
    base = dict(
        drive_id="northwind:sde:2026", company="Northwind Systems", role="Software Engineer",
        version=1, criteria=Criteria(min_gpa=7.0), last_verdict="ELIGIBLE",
        jd_text="Looking for a backend engineer with Django experience.",
    )
    base.update(overrides)
    return Drive(**base)


def _bot(db_path, *, files=None, settings=None) -> tuple[TelegramBotLoop, Adapters]:
    adapters = Adapters(
        sheets=FakeSheetStore(PROFILE, None), calendar=FakeCalendarStore(), messenger=FakeMessenger(), files=files,
    )
    return TelegramBotLoop("token", "chat1", db_path, adapters, settings=settings), adapters


def _plan_choice_card(db_path, drive, adapters) -> dict:
    """Plans AND executes the resume-choice card (mirroring the real worker
    loop always calling run_pending right after process_message) so it has
    an actual prior send/message_id for the resolution step to edit -- a
    card only ever planned but never sent has nothing to edit yet."""
    planner._plan_resume_choice(db_path, drive, new_epoch=False)
    executor.run_pending(db_path, adapters)
    return executor.get_pending_approval(db_path, drive.drive_id)


def _settings(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        llm_api_key="unused", llm_model="stub", llm_provider="anthropic", llm_base_url=None,
        generated_resume_dir=str(tmp_path / "generated"), public_base_url="http://127.0.0.1:8000",
        master_profile_path=str(tmp_path / "master_profile.yaml"),
    )


def test_use_my_resume_runs_the_static_match_path(tmp_path, monkeypatch):
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)
    drive = _drive()
    resolve.save_drive(db_path, drive)
    files = FakeFileStore([ResumeFile(file_id="f1", name="resume_SDE.pdf", web_view_link="https://drive/sde")])
    bot, adapters = _bot(db_path, files=files, settings=_settings(tmp_path))
    approval = _plan_choice_card(db_path, drive, adapters)

    monkeypatch.setattr("cutoff.pipeline.master_profile.load_master_profile", lambda path: None)
    called_generate = []
    monkeypatch.setattr("cutoff.llm.resume_generate.generate_tailored_resume",
                        lambda *a, **k: called_generate.append(1))
    monkeypatch.setattr(
        "cutoff.llm.resume_match.select_best_resume",
        lambda jd_text, resumes, **k: ("resume_SDE.pdf", "Matches on backend experience."),
    )
    monkeypatch.setattr("cutoff.pipeline.resume.extract_pdf_text", lambda data: "some resume text")

    bot._handle_resume_choice(approval, "use")

    assert not called_generate  # "use" must never even attempt generation
    edit_calls = [c for c in adapters.messenger.calls if c[0] == "edit"]
    assert len(edit_calls) == 1
    assert "drive/sde" in edit_calls[0][1]["text"]
    assert "Matches on backend experience." in edit_calls[0][1]["text"]
    saved = resolve.get_drive(db_path, drive.drive_id)
    assert saved.resolved_resume_pick["name"] == "resume_SDE.pdf"


def test_generate_tailored_resume_runs_only_on_generate(tmp_path, monkeypatch):
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)
    drive = _drive()
    resolve.save_drive(db_path, drive)
    files = FakeFileStore([])
    master_profile = MasterProfile(skills=["Django"], projects=[MasterProfileProject(title="X", bullets=["Y"])])
    bot, adapters = _bot(db_path, files=files, settings=_settings(tmp_path))
    approval = _plan_choice_card(db_path, drive, adapters)

    monkeypatch.setattr("cutoff.pipeline.master_profile.load_master_profile", lambda path: master_profile)
    monkeypatch.setattr(
        "cutoff.llm.resume_generate.generate_tailored_resume",
        lambda jd_text, profile, **k: (
            {"headline": "H", "skills": ["Django"], "highlighted_projects": [{"title": "X", "bullets": ["Y"]}],
             "match_reason": "Great fit."},
            "Great fit.",
        ),
    )
    monkeypatch.setattr("cutoff.pipeline.resume_pdf.render_resume_pdf", lambda *a, **k: b"%PDF-1.4 fake")

    bot._handle_resume_choice(approval, "generate")

    edit_calls = [c for c in adapters.messenger.calls if c[0] == "edit"]
    assert "Great fit." in edit_calls[0][1]["text"]
    assert "/generated_resumes/" in edit_calls[0][1]["text"]


def test_resume_choice_degrades_gracefully_with_no_filestore_wired(tmp_path):
    """No FileStore on Adapters (e.g. a bot loop constructed without it) must
    never crash -- the student's tap still resolves, just with no resume."""
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)
    drive = _drive()
    resolve.save_drive(db_path, drive)
    bot, adapters = _bot(db_path, files=None, settings=_settings(tmp_path))
    approval = _plan_choice_card(db_path, drive, adapters)

    bot._handle_resume_choice(approval, "use")

    edit_calls = [c for c in adapters.messenger.calls if c[0] == "edit"]
    assert len(edit_calls) == 1
    assert "Register?" in edit_calls[0][1]["text"]
    assert "none on file" in edit_calls[0][1]["text"]
    saved = resolve.get_drive(db_path, drive.drive_id)
    assert saved.resolved_resume_pick == {"resolved": True, "file_id": None, "name": None,
                                           "web_view_link": None, "reason": None}


def test_resume_choice_degrades_gracefully_with_no_settings_wired(tmp_path):
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)
    drive = _drive()
    resolve.save_drive(db_path, drive)
    files = FakeFileStore([])
    bot, adapters = _bot(db_path, files=files, settings=None)
    approval = _plan_choice_card(db_path, drive, adapters)

    bot._handle_resume_choice(approval, "generate")

    edit_calls = [c for c in adapters.messenger.calls if c[0] == "edit"]
    assert len(edit_calls) == 1  # still resolves the card, just with no resume


def test_resume_choice_is_a_no_op_once_already_decided(tmp_path):
    """Double-tapping (or a stale button after a resend) must not re-resolve
    or send a second edit."""
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)
    drive = _drive()
    resolve.save_drive(db_path, drive)
    files = FakeFileStore([])
    bot, adapters = _bot(db_path, files=files, settings=_settings(tmp_path))
    approval = _plan_choice_card(db_path, drive, adapters)
    executor.void_pending_approvals(db_path, drive.drive_id)  # already resolved by some other path
    approval["status"] = "VOIDED"
    adapters.messenger.calls.clear()  # the choice card's own initial send doesn't count here

    bot._handle_resume_choice(approval, "generate")

    assert adapters.messenger.calls == []


def test_resend_of_a_resume_choice_card_refreshes_every_button_kind(tmp_path):
    """Regression: resend_due_reminders used to only rewrite "a:{token}:"
    callback_data, silently leaving a resent resume-choice card's
    "r:{token}:use"/"r:{token}:generate" buttons pointing at a stale,
    no-longer-PENDING token."""
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)
    drive = _drive()
    resolve.save_drive(db_path, drive)
    bot, adapters = _bot(db_path, files=FakeFileStore([]), settings=_settings(tmp_path))
    approval = _plan_choice_card(db_path, drive, adapters)
    old_token = approval["approval_id"]
    executor.snooze_approval(db_path, old_token, NOW)
    adapters.messenger.calls.clear()

    resent = bot.resend_due_reminders(now=NOW + timedelta(hours=2, minutes=1))

    assert resent == 1
    send_calls = [c for c in adapters.messenger.calls if c[0] == "send"]
    new_buttons = send_calls[0][1]["buttons"]
    tokens = {b.callback_data.split(":")[1] for b in new_buttons}
    assert tokens == {executor.get_pending_approval(db_path, drive.drive_id)["approval_id"]}
    assert old_token not in tokens
    kinds = {b.callback_data.split(":")[0] for b in new_buttons}
    assert kinds == {"r", "a"}

"""Entrypoint: `python -m cutoff.main` starts FastAPI + the worker thread +
the Telegram long-poll thread, all in one process (Section 5).

MODE=fake (default): fakes with no built-in source of new mail — the
dashboard starts empty until Phase 5's demo endpoints exist to feed it.
MODE=real: real Gmail/Sheets/Calendar/Drive/Telegram, per Section 6.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import uvicorn

from cutoff import db, trace
from cutoff.adapters.base import CalendarStore, FileStore, MailSource, Messenger, SheetStore
from cutoff.config import Settings, get_settings
from cutoff.llm.extract import extract_notice
from cutoff.pipeline import executor, form_autofill, run
from cutoff.pipeline.executor import Adapters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logger = logging.getLogger("cutoff.main")

_stop_event = threading.Event()

# Section 6.1/12's "second, broader query" — recruiting-specific phrases,
# scoped narrow enough that a real inbox's ordinary mail volume doesn't
# paginate through hundreds of unrelated results every poll. This is what
# lets a lookalike-domain or spoofed sender ever reach security.py's
# lookalike/spoof checks at all (list_new is deliberately sender-scoped).
SUSPICIOUS_QUERY_KEYWORDS = [
    "campus drive", "placement", "recruiting", "recruitment", "shortlist",
    "registration deadline", "eligibility criteria", "career office",
]


def _load_master_profile(settings: Settings):
    from cutoff.pipeline.master_profile import load_master_profile

    return load_master_profile(settings.master_profile_path)


@dataclass
class AppAdapters:
    mail: MailSource
    sheets: SheetStore
    calendar: CalendarStore
    files: FileStore
    messenger: Messenger


def _build_fake_adapters() -> AppAdapters:
    from cutoff.adapters.fakes import FakeCalendarStore, FakeFileStore, FakeMailSource, FakeMessenger, FakeSheetStore
    from cutoff.models import CollegePolicy, StudentProfile

    profile = StudentProfile(
        name="Student", roll_no="00000000", email="student@example.edu", branch="CSE",
        gpa=7.5, active_backlogs=0, batch_year=2026,
    )
    policy = CollegePolicy(college_domain="example.edu")
    return AppAdapters(
        mail=FakeMailSource(), sheets=FakeSheetStore(profile, policy),
        calendar=FakeCalendarStore(), files=FakeFileStore([]), messenger=FakeMessenger(),
    )


def _build_real_adapters(settings: Settings) -> AppAdapters:
    from cutoff.adapters.calendar_real import CalendarSource
    from cutoff.adapters.drive_real import DriveSource
    from cutoff.adapters.gmail_real import GmailSource
    from cutoff.adapters.google_auth import build_service, load_credentials
    from cutoff.adapters.sheets_real import SheetsSource
    from cutoff.adapters.telegram_real import TelegramMessenger

    creds = load_credentials(settings.google_client_secret_file, settings.google_token_file)
    gmail_service = build_service("gmail", "v1", creds, settings.gmail_api_base_url or None)
    sheets_service = build_service("sheets", "v4", creds, settings.sheets_api_base_url or None)
    calendar_service = build_service("calendar", "v3", creds, settings.calendar_api_base_url or None)
    drive_service = build_service("drive", "v3", creds, settings.drive_api_base_url or None)

    return AppAdapters(
        mail=GmailSource(gmail_service),
        sheets=SheetsSource(sheets_service, settings.sheet_id),
        calendar=CalendarSource(calendar_service, settings.calendar_id, settings.exam_calendar_id or None),
        files=DriveSource(drive_service, settings.resume_folder_id),
        messenger=TelegramMessenger(settings.telegram_bot_token, settings.telegram_chat_id),
    )


def _build_adapters(settings: Settings) -> AppAdapters:
    return _build_real_adapters(settings) if settings.mode == "real" else _build_fake_adapters()


def _worker_loop(settings: Settings, adapters: AppAdapters) -> None:
    logger.info("worker thread started (poll every %ss, MODE=%s)", settings.poll_interval_seconds, settings.mode)

    if settings.mode != "real":
        # No live mail source to poll in fake mode — Phase 5's demo endpoints
        # feed the pipeline instead. Just heartbeat so the thread shape holds.
        while not _stop_event.is_set():
            logger.debug("worker heartbeat (fake mode)")
            _stop_event.wait(settings.poll_interval_seconds)
        logger.info("worker thread stopped")
        return

    ctx = run.PipelineContext(
        mail=adapters.mail, files=adapters.files, extract_fn=extract_notice, api_key=settings.llm_api_key,
        model=settings.llm_model, timezone_name=settings.timezone, db_path=settings.db_path,
        provider=settings.llm_provider,
        base_url=settings.llm_base_url,
        calendar=adapters.calendar,
        sheets=adapters.sheets,
        autofill_form_fn=form_autofill.build_autofilled_url,
        master_profile=_load_master_profile(settings),
        generated_resume_dir=settings.generated_resume_dir,
        public_base_url=settings.public_base_url,
        enable_prep_intel=settings.enable_prep_intel,
    )
    exec_adapters = Adapters(
        sheets=adapters.sheets, calendar=adapters.calendar, messenger=adapters.messenger, files=adapters.files,
    )
    # Shared across every poll (not a fresh one each time) so a circuit
    # actually remembers a run of failures across polls, and so the
    # dashboard's health panel can read the same live state (Section 16
    # extension — see NOTES.md).
    breaker = executor.shared_breaker(settings.db_path)
    # 2-minute overlap on every poll: Gmail's own indexing can lag a query by
    # a few seconds, so a message received right at the last poll's boundary
    # could otherwise be missed. processed_messages already dedupes re-fetches.
    since = datetime.now(timezone.utc) - timedelta(minutes=5)

    while not _stop_event.is_set():
        try:
            ctx.master_profile = _load_master_profile(settings)
            profile = adapters.sheets.read_profile()
            policy = adapters.sheets.read_policy()
            now = datetime.now(timezone.utc)

            seen_message_ids: set[str] = set()
            for ref in adapters.mail.list_new(policy.career_office_senders, since):
                seen_message_ids.add(ref.message_id)
                msg = adapters.mail.get(ref.message_id)
                run.process_message(msg, ctx, profile=profile, policy=policy, now=now)

            # Section 6.1/12's "second, broader query": keyword-scoped, no
            # sender restriction — the only way a lookalike-domain or
            # spoofed-display-name sender ever reaches security.py's
            # check_lookalike/check_display_spoof at all, since list_new
            # above is deliberately narrowed to the allowlist. Found live:
            # that check existed all along but could never fire in real
            # polling. Scoped by keyword, not "everyone" — an unscoped query
            # against a real inbox's normal mail volume paginates through
            # hundreds of unrelated messages every poll and starves the
            # loop (found live, before this was keyword-scoped).
            for ref in adapters.mail.list_suspicious(SUSPICIOUS_QUERY_KEYWORDS, since):
                if ref.message_id in seen_message_ids:
                    continue  # already handled via list_new above
                msg = adapters.mail.get(ref.message_id)
                run.process_message(msg, ctx, profile=profile, policy=policy, now=now)

            executor.run_pending(settings.db_path, exec_adapters, breaker)
            since = now - timedelta(minutes=2)
        except Exception:
            logger.exception("worker loop iteration failed; continuing")
        _stop_event.wait(settings.poll_interval_seconds)

    logger.info("worker thread stopped")


def _telegram_loop(settings: Settings, adapters: AppAdapters) -> None:
    if settings.mode != "real":
        logger.info("telegram thread started (stub — MODE=fake has no live bot to poll)")
        while not _stop_event.is_set():
            _stop_event.wait(5)
        logger.info("telegram thread stopped")
        return

    from cutoff.bot.telegram_loop import TelegramBotLoop

    exec_adapters = Adapters(
        sheets=adapters.sheets, calendar=adapters.calendar, messenger=adapters.messenger, files=adapters.files,
    )
    bot = TelegramBotLoop(
        settings.telegram_bot_token, settings.telegram_chat_id, settings.db_path, exec_adapters, settings=settings,
    )
    bot.run_forever(_stop_event)


def main() -> None:
    settings = get_settings()
    db.init_db(settings.db_path)
    trace.configure(settings.db_path)
    adapters = _build_adapters(settings)

    worker = threading.Thread(target=_worker_loop, args=(settings, adapters), daemon=True)
    telegram = threading.Thread(target=_telegram_loop, args=(settings, adapters), daemon=True)
    worker.start()
    telegram.start()

    logger.info("starting FastAPI on http://127.0.0.1:8000 (MODE=%s)", settings.mode)
    try:
        uvicorn.run("cutoff.app.server:app", host="127.0.0.1", port=8000, log_level="info")
    finally:
        _stop_event.set()
        worker.join(timeout=2)
        telegram.join(timeout=2)


if __name__ == "__main__":
    main()

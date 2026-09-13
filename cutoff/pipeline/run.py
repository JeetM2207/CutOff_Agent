"""`process_message`: one run per email (Section 8). Ends once actions are
persisted to the ledger — execution happens separately in `executor.run_pending`."""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from cutoff import db, trace
from cutoff.adapters.base import CalendarStore, FileStore, MailSource, SheetStore
from cutoff.llm import grounding
from cutoff.llm.extract import EXTRACTION_FAILED_SENTINEL
from cutoff.models import CollegePolicy, EmailMessage, Notice, StudentProfile
from cutoff.pipeline import clash, eligibility, executor, form_prefill, ingest, planner, resolve, security, shortlist, timeparse
from cutoff.pipeline import resume as resume_mod

ExtractFn = Callable[..., Notice]
AutofillFormFn = Callable[..., str | None]


@dataclass
class PipelineContext:
    mail: MailSource
    files: FileStore
    extract_fn: ExtractFn
    api_key: str
    model: str
    timezone_name: str
    db_path: str
    use_llm_cache: bool = True
    provider: str = "anthropic"  # "anthropic" | "openrouter" — see cutoff.llm.client
    base_url: str | None = None
    # Read-only (Section 11.2: reads aren't ledgered, may happen inline) — used
    # only for exam-clash lookups (Section 11.3) when handling a SCHEDULE notice.
    calendar: CalendarStore | None = None
    # Read-only, same rationale as `calendar` — used only to look up a stored
    # pre-filled form template (Section 6.5 extension) for the ELIGIBLE
    # NEW_DRIVE/REVISION answer card.
    sheets: SheetStore | None = None
    # Optional (Section 0 rule 3: a test-constructed context must never make
    # a real network/LLM call without an explicit stub) — auto-detects a
    # Google Form's own fields and fills them (Section 6.5 extension). None
    # disables the feature entirely: falls straight to the stored-template
    # lookup above, same as before this existed. main.py wires the real
    # cutoff.pipeline.form_autofill.build_autofilled_url here.
    autofill_form_fn: AutofillFormFn | None = None
    # Dynamic resume generation (Section 6.4 extension). None/empty
    # master_profile_md disables it entirely — same "must never block
    # planning" guarantee as autofill_form_fn above; select_resume_smart
    # falls straight back to static resume_*.pdf matching. A test-constructed
    # context that doesn't set these (i.e. every test written before this
    # feature existed) gets the exact prior behavior, unchanged.
    master_profile_md: str | None = None
    generated_resume_dir: str | None = None
    public_base_url: str = ""


@dataclass
class RunResult:
    run_id: str
    message_id: str
    notice_type: str | None
    drive_id: str | None
    verdict: str | None
    actions_planned: int
    note: str
    unverified_fields: list[str] = field(default_factory=list)


def _mark_processed(db_path: str, message_id: str, run_id: str) -> bool:
    """True if newly marked; False if `message_id` was already processed
    (duplicate delivery, Section 6.1 / 8 step 1) — then this run is a no-op."""
    conn = db.get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO processed_messages (message_id, processed_at, run_id) VALUES (?, ?, ?)",
            (message_id, datetime.now(timezone.utc).isoformat(), run_id),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def process_message(msg: EmailMessage, ctx: PipelineContext, *, profile: StudentProfile,
                     policy: CollegePolicy, now: datetime) -> RunResult:
    run_id = uuid.uuid4().hex
    trace.configure(ctx.db_path)
    with trace.run(run_id):
        with trace.span("ingest", message_id=msg.message_id):
            if not _mark_processed(ctx.db_path, msg.message_id, run_id):
                return RunResult(run_id, msg.message_id, None, None, None, 0, "duplicate message_id: no-op")
            ingested = ingest.ingest(msg, ctx.mail)

        with trace.span("security", from_addr=msg.from_addr):
            allowed, sender_signals = security.check_sender(msg.from_addr, policy)
            if not allowed:
                signals = sender_signals + security.scan_content(ingested.full_text)
                if signals:
                    planner.plan_suspicious(
                        ctx.db_path, key=f"tg:suspicious:{msg.message_id}", drive=None,
                        company_hint=msg.subject, signals=signals,
                    )
                    return RunResult(run_id, msg.message_id, "SUSPICIOUS", None, None, 1,
                                      "non-allowlisted sender, scam signals found")
                return RunResult(run_id, msg.message_id, None, None, None, 0,
                                  "non-allowlisted sender, no scam signals: ignored")

        with trace.span("extract", model=ctx.model):
            notice = ctx.extract_fn(
                msg, ingested.body_text, ingested.quoted_history, ingested.attachment_text,
                api_key=ctx.api_key, model=ctx.model, db_path=ctx.db_path,
                timezone_name=ctx.timezone_name, use_cache=ctx.use_llm_cache,
                provider=ctx.provider, base_url=ctx.base_url,
            )

        if EXTRACTION_FAILED_SENTINEL in notice.unverified_fields:
            with trace.span("plan", reason="extraction_failed"):
                planner.plan_suspicious(
                    ctx.db_path, key=f"tg:unreadable:{msg.message_id}", drive=None,
                    company_hint=msg.subject, signals=["I couldn't read this email reliably."],
                    kind="unreadable",
                )
            return RunResult(run_id, msg.message_id, notice.notice_type, None, "NEEDS_REVIEW", 1,
                              "extraction failed twice", unverified_fields=notice.unverified_fields)

        with trace.span("validate", notice_type=notice.notice_type):
            # Evidence quotes may come from the subject line too (prompt rule 3).
            grounding_source = f"{msg.subject}\n{ingested.full_text}"
            notice = grounding.validate_grounding(notice, grounding_source)
            reparsed = timeparse.resolve_deadline(notice.deadline_text, msg.received_at, profile.timezone)
            if not timeparse.deadlines_agree(notice.deadline, reparsed):
                unverified = notice.unverified_fields if "deadline" in notice.unverified_fields else [
                    *notice.unverified_fields, "deadline",
                ]
                notice = notice.model_copy(update={"deadline": None, "unverified_fields": unverified})
            if not security.check_form_domain(notice.form_url, policy):
                notice = notice.model_copy(
                    update={"suspicion_signals": [*notice.suspicion_signals, "FORM_OFF_DOMAIN"]}
                )

        if notice.notice_type == "NON_DRIVE":
            return RunResult(run_id, msg.message_id, "NON_DRIVE", None, None, 0, "not a drive email",
                              unverified_fields=notice.unverified_fields)

        if notice.notice_type == "SUSPICIOUS":
            with trace.span("plan", reason="suspicious_notice"):
                planner.plan_suspicious(
                    ctx.db_path, key=f"tg:suspicious:{msg.message_id}", drive=None,
                    company_hint=notice.company or msg.subject,
                    signals=notice.suspicion_signals or ["suspicious content"],
                )
            return RunResult(run_id, msg.message_id, "SUSPICIOUS", None, None, 1,
                              "flagged suspicious by extraction", unverified_fields=notice.unverified_fields)

        season = str(notice.criteria.batch_years[0]) if notice.criteria.batch_years else str(profile.batch_year)

        with trace.span("resolve", notice_type=notice.notice_type):
            resolved = resolve.resolve_drive(ctx.db_path, msg.thread_id, notice, season)
            if resolved.drive is None and resolved.method in ("fuzzy_ambiguous", "none"):
                planner.plan_suspicious(
                    ctx.db_path, key=f"tg:unresolved:{msg.message_id}", drive=None,
                    company_hint=notice.company or msg.subject,
                    signals=[resolved.needs_review_question or "couldn't resolve which drive this is about"],
                    kind="unresolved",
                )
                return RunResult(run_id, msg.message_id, notice.notice_type, None, "NEEDS_REVIEW", 1,
                                  "drive resolution ambiguous", unverified_fields=notice.unverified_fields)
            existing = resolved.drive

        previous_verdict = existing.last_verdict if existing else None

        with trace.span("resolve:apply", drive_id=(existing.drive_id if existing else None)):
            drive, diff, changed = resolve.apply_notice(existing, notice, msg.message_id, season)
            resolve.link_thread(ctx.db_path, msg.thread_id, drive.drive_id)
            # A SHORTLIST notice's payload (the PDF) lives outside Drive's own
            # fields entirely — it almost never changes criteria/deadline/etc.,
            # but still needs shortlist.match() to run, so it's exempt from
            # the "nothing changed" short-circuit REMINDER/REVISION rely on.
            if not changed and notice.notice_type != "SHORTLIST":
                return RunResult(run_id, msg.message_id, notice.notice_type, drive.drive_id, previous_verdict, 0,
                                  f"{notice.notice_type.lower()}: no changes", unverified_fields=notice.unverified_fields)

        with trace.span("eligibility", drive_id=drive.drive_id):
            verdict = eligibility.evaluate(drive, notice, profile, policy, now)
            dstate = timeparse.deadline_state(drive.deadline, now)
            drive.last_verdict = verdict.result
            resolve.save_drive(ctx.db_path, drive)
            # Evidence quotes ride along in the diff (not a separate table)
            # purely so the dashboard's drive-detail view (Section 16) can
            # show "every extracted field, with the exact words it came
            # from" without a schema change.
            diff["evidence"] = [e.model_dump(mode="json") for e in notice.evidence]
            diff["verdict"] = verdict.model_dump(mode="json")
            resolve.record_history(ctx.db_path, drive.drive_id, drive.version, diff, msg.message_id)

        event_clash_warnings = None
        if notice.notice_type == "SCHEDULE" and ctx.calendar is not None:
            with trace.span("clash", drive_id=drive.drive_id):
                event_clash_warnings = [
                    clash.clash_warning(clash.find_clashes(ctx.calendar, ev)) for ev in drive.events
                ]

        shortlist_result = None
        if notice.notice_type == "SHORTLIST":
            with trace.span("shortlist", drive_id=drive.drive_id):
                pdf_attachment = next(
                    (a for a in msg.attachments if a.mime_type == "application/pdf" or a.filename.lower().endswith(".pdf")),
                    None,
                )
                if pdf_attachment is not None:
                    # Reads aren't ledgered (Section 11.2) but still need the
                    # same retry/backoff as writes — found under chaos
                    # testing: a single injected 429 on this call killed the
                    # whole message with no retry at all.
                    ok, pdf_bytes, err = executor.with_retry(
                        lambda: ctx.mail.get_attachment(msg.message_id, pdf_attachment.attachment_id)
                    )
                    if not ok:
                        raise RuntimeError(f"couldn't fetch shortlist PDF attachment: {err}")
                    shortlist_result = shortlist.match(pdf_bytes, profile.roll_no, profile.name)

        with trace.span("plan", drive_id=drive.drive_id, verdict=verdict.result):
            # JD-content resume matching only matters where planner.plan()
            # actually shows a resume (the ELIGIBLE NEW_DRIVE/REVISION answer
            # card) — everywhere else this stays the cheap, deterministic,
            # zero-LLM-call category lookup, so SHORTLIST's own PDF attachment
            # (extracted into the same ingested.attachment_text) never gets
            # mistaken for a job description.
            prefilled_form_url = None
            if (notice.notice_type in ("NEW_DRIVE", "REVISION")
                    and verdict.result == "ELIGIBLE" and dstate != "PASSED"):
                resume_file, resume_reason = resume_mod.select_resume_smart(
                    drive.role_category, ingested.attachment_text, ctx.files,
                    api_key=ctx.api_key, model=ctx.model, provider=ctx.provider,
                    base_url=ctx.base_url, db_path=ctx.db_path,
                    student_profile=profile, master_profile_md=ctx.master_profile_md,
                    generated_resume_dir=ctx.generated_resume_dir, public_base_url=ctx.public_base_url,
                )

                autofilled_url = None
                if ctx.autofill_form_fn is not None and drive.form_url:
                    resume_text = ""
                    if resume_file is not None:
                        ok, data, err = executor.with_retry(
                            lambda: ctx.files.get_resume_content(resume_file.file_id)
                        )
                        if ok:
                            try:
                                resume_text = ingest.extract_pdf_text(data)
                            except Exception:
                                resume_text = ""
                    try:
                        autofilled_url = ctx.autofill_form_fn(
                            drive.form_url, profile, resume_text, ingested.attachment_text,
                            resume_link=(resume_file.web_view_link if resume_file else None),
                            api_key=ctx.api_key, model=ctx.model, provider=ctx.provider, base_url=ctx.base_url,
                        )
                    except Exception:
                        autofilled_url = None

                if autofilled_url:
                    prefilled_form_url = autofilled_url
                elif ctx.sheets is not None and drive.form_url:
                    try:
                        template = ctx.sheets.read_form_template(drive.form_url)
                    except Exception:
                        template = None  # e.g. FormTemplates tab doesn't exist yet — plain link is fine
                    prefilled_form_url = form_prefill.build_prefilled_url(template, profile)
            else:
                resume_file, resume_reason = resume_mod.select_resume(drive.role_category, ctx.files), None
            actions = planner.plan(
                ctx.db_path, drive=drive, previous_verdict=previous_verdict, notice=notice,
                verdict=verdict, deadline_state=dstate, profile=profile, resume=resume_file,
                resume_reason=resume_reason, prefilled_form_url=prefilled_form_url,
                event_clash_warnings=event_clash_warnings, shortlist_result=shortlist_result,
            )

    return RunResult(run_id, msg.message_id, notice.notice_type, drive.drive_id, verdict.result,
                      len(actions), "ok", unverified_fields=notice.unverified_fields)

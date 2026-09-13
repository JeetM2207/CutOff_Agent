"""Long-polling Telegram bot loop (Section 6.5): getUpdates -> callback_query
dispatch. Only accepts callbacks from TELEGRAM_CHAT_ID. Registration is the
one T2 action (Section 11.2) — the agent never submits the form; tapping
"Mark submitted" is the human's own confirmation that they did."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

import httpx

from cutoff import db, trace
from cutoff.models import Action, ResumeFile
from cutoff.pipeline import executor, planner, resolve
from cutoff.pipeline import resume as resume_mod
from cutoff.pipeline.executor import Adapters

logger = logging.getLogger("cutoff.bot")

POLL_TIMEOUT = 25


class TelegramBotLoop:
    def __init__(self, bot_token: str, chat_id: str, db_path: str, adapters: Adapters, *, settings=None):
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._chat_id = str(chat_id)
        self._db_path = db_path
        self._adapters = adapters
        self._offset: int | None = None
        # Section 6.4 extension (dynamic resume generation): only needed to
        # resolve a "use my resume / generate one" callback. None disables
        # that specific callback gracefully (see _handle_resume_choice) --
        # every existing caller (including every test written before this
        # feature existed) that doesn't pass it is otherwise unaffected.
        self._settings = settings

    def run_forever(self, stop_event) -> None:
        logger.info("telegram bot loop started")
        while not stop_event.is_set():
            try:
                self.resend_due_reminders()
            except Exception:
                # A bug here must never take down getUpdates polling itself —
                # worst case a reminder is late, not the whole bot going dark.
                logger.exception("resend_due_reminders failed; continuing")
            try:
                self._poll_once()
            except httpx.HTTPError:
                logger.exception("telegram getUpdates failed; retrying")
                stop_event.wait(2)
        logger.info("telegram bot loop stopped")

    def _poll_once(self) -> None:
        params = {"timeout": POLL_TIMEOUT}
        if self._offset is not None:
            params["offset"] = self._offset
        resp = httpx.get(f"{self._base}/getUpdates", params=params, timeout=POLL_TIMEOUT + 5)
        resp.raise_for_status()
        for update in resp.json().get("result", []):
            self._offset = update["update_id"] + 1
            cq = update.get("callback_query")
            if cq:
                self._handle_callback(cq)

    # --- dispatch -----------------------------------------------------------

    def _handle_callback(self, cq: dict) -> None:
        callback_id = cq["id"]
        chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
        if chat_id != self._chat_id:
            self._answer(callback_id, "")  # Section 6.5: only TELEGRAM_CHAT_ID is trusted
            return

        self._answer(callback_id, "")  # ack immediately, regardless of outcome below

        data = cq.get("data", "")
        parts = data.split(":")
        trace.configure(self._db_path)
        with trace.span("telegram_callback", data=data):
            if len(parts) == 2 and parts[0] == "s":
                return  # "Show why" on a suspicious warning: nothing further to resolve
            if len(parts) != 3:
                logger.warning("unrecognized callback_data: %r", data)
                return
            kind, token, action = parts
            if kind == "m":
                self.handle_mark_submitted(token)
                return
            approval = self._get_approval(token)
            if approval is None:
                logger.warning("callback for unknown token %r", token)
                return
            if kind == "a":
                self._handle_approval(approval, action)
            elif kind == "q":
                self._handle_question(approval, action)
            elif kind == "r":
                self._handle_resume_choice(approval, action)

    def _answer(self, callback_id: str, text: str) -> None:
        httpx.post(f"{self._base}/answerCallbackQuery",
                   json={"callback_query_id": callback_id, "text": text}, timeout=10)

    def _get_approval(self, token: str) -> dict | None:
        conn = db.get_connection(self._db_path)
        row = conn.execute("SELECT * FROM approvals WHERE approval_id = ?", (token,)).fetchone()
        return dict(row) if row else None

    # --- approval flow (Approve / Skip / Remind) -------------------------------

    def _handle_approval(self, approval: dict, action: str) -> None:
        if approval["status"] != "PENDING":
            return  # already decided (voided by a revision, or acted on already)

        if action == "approve":
            self._void_approval(approval["approval_id"])  # this approval's decision is made
            self._send_mark_submitted_prompt(approval)
        elif action == "skip":
            self._void_approval(approval["approval_id"])
            self._edit_original(approval, "Skipped. You can still register manually before the deadline.")
        elif action == "snooze":
            executor.snooze_approval(self._db_path, approval["approval_id"], datetime.now(timezone.utc))
            self._edit_original(approval, "OK, I'll ask again in 2 hours. (The deadline reminder on your calendar still stands either way.)")

    def _send_mark_submitted_prompt(self, approval: dict) -> None:
        drive = resolve.get_drive(self._db_path, approval["drive_id"])
        if drive is None:
            return
        new_token = executor.new_short_token()
        text = f"Go ahead and submit the form for {drive.company} ({drive.role}). Tap below once you've submitted it."
        buttons = [{"text": "Mark submitted", "callback_data": f"m:{new_token}:submitted"}]
        planned = self._plan_and_run_message(
            drive, key=f"tg:{drive.drive_id}:submit:v{drive.version}", text=text, buttons=buttons
        )
        executor.create_approval(self._db_path, action_id=planned.action_id, drive_id=drive.drive_id,
                                  drive_version=drive.version, approval_id=new_token)

    def resend_due_reminders(self, now: datetime | None = None) -> int:
        """"Remind me in 2h" (Section 6.5): re-sends the original approval
        card for anything whose 2-hour snooze has elapsed, under a fresh
        token. Called once per worker poll (cutoff.main), same cadence as
        everything else here. If the drive changed since the snooze (a
        correction landed), the stale approval is dropped instead of
        resending outdated info — a fresh one for the new version already
        exists. `now` is injectable so tests never depend on the real clock.
        Returns how many were resent."""
        conn = db.get_connection(self._db_path)
        now = now or datetime.now(timezone.utc)
        resent = 0
        for approval in executor.get_due_snoozed_approvals(self._db_path, now):
            action_row = conn.execute(
                "SELECT payload FROM actions WHERE action_id = ?", (approval["action_id"],)
            ).fetchone()
            drive = resolve.get_drive(self._db_path, approval["drive_id"])
            if action_row is None or drive is None or drive.version != approval["drive_version"]:
                conn.execute("UPDATE approvals SET status = 'VOIDED' WHERE approval_id = ?", (approval["approval_id"],))
                conn.commit()
                continue

            payload = json.loads(action_row["payload"])
            old_token, new_token = approval["approval_id"], executor.new_short_token()
            # Replace the bare token, not "a:{token}:" specifically — a
            # resent card can carry buttons of more than one kind (e.g. the
            # resume-choice card's "r:{token}:generate" alongside its
            # "a:{token}:skip"/"a:{token}:snooze"), and every one needs the
            # same fresh token or a tap on the stale ones would silently do
            # nothing (found while adding the resume-choice card).
            buttons = [
                {**b, "callback_data": b["callback_data"].replace(f":{old_token}:", f":{new_token}:")}
                for b in payload.get("buttons") or []
            ]
            planned = self._plan_and_run_message(
                drive, key=f"tg:{drive.drive_id}:remind:{new_token}", text=payload["text"], buttons=buttons
            )
            executor.create_approval(self._db_path, action_id=planned.action_id, drive_id=drive.drive_id,
                                      drive_version=drive.version, approval_id=new_token)
            conn.execute("UPDATE approvals SET status = 'RESENT' WHERE approval_id = ?", (old_token,))
            conn.commit()
            resent += 1
        return resent

    # --- resume choice flow (Use my resume / Generate tailored) --------------
    # Section 6.4 extension: the student explicitly picks how their resume
    # gets handled for this drive, over a Telegram button, rather than the
    # agent silently deciding. See planner._plan_resume_choice for how this
    # card gets sent in the first place.

    def _handle_resume_choice(self, approval: dict, action: str) -> None:
        if approval["status"] != "PENDING":
            return
        drive = resolve.get_drive(self._db_path, approval["drive_id"])
        if drive is None or action not in ("use", "generate"):
            return

        self._void_approval(approval["approval_id"])  # the resume question is answered
        profile = self._adapters.sheets.read_profile()
        resume_file, resume_reason = self._resolve_resume(drive, profile, mode=action)

        drive.resolved_resume_pick = {
            "resolved": True,
            "file_id": resume_file.file_id if resume_file else None,
            "name": resume_file.name if resume_file else None,
            "web_view_link": resume_file.web_view_link if resume_file else None,
            "reason": resume_reason,
        }
        resolve.save_drive(self._db_path, drive)

        prefilled_form_url = self._resolve_prefilled_form_url(drive, profile, resume_file)
        new_epoch = self._new_epoch_for_action(approval["action_id"])
        planned = planner.plan_resolved_approval(
            self._db_path, drive, profile, resume_file, new_epoch=new_epoch,
            resume_reason=resume_reason, prefilled_form_url=prefilled_form_url,
        )
        executor.run_pending(self._db_path, self._adapters)
        logger.info("resume choice %r resolved for drive %s (action %s)", action, drive.drive_id, planned.action_id)

    def _resolve_resume(self, drive, profile, *, mode: str) -> tuple[ResumeFile | None, str | None]:
        """Runs the static/generation resume logic in a FORCED mode (never
        "auto" — the student already made the choice) directly from the bot
        loop, since this happens well after run.process_message's own single
        pass. Never raises: any failure here degrades to (None, None) rather
        than leaving the student's tap unanswered."""
        if self._adapters.files is None:
            logger.warning("resume choice %r requested but no FileStore is wired to this bot loop", mode)
            return None, None
        if self._settings is None:
            logger.warning("resume choice %r requested but TelegramBotLoop has no Settings", mode)
            return None, None

        from cutoff.pipeline.master_profile import load_master_profile

        try:
            master_profile = load_master_profile(self._settings.master_profile_path)
            return resume_mod.select_resume_smart(
                drive.role_category, drive.jd_text or "", self._adapters.files,
                api_key=self._settings.llm_api_key, model=self._settings.llm_model,
                provider=self._settings.llm_provider, base_url=self._settings.llm_base_url,
                db_path=self._db_path, student_profile=profile, master_profile=master_profile,
                generated_resume_dir=self._settings.generated_resume_dir,
                public_base_url=self._settings.public_base_url, resume_mode=mode,
            )
        except Exception:
            logger.exception("resume resolution failed for drive %s (mode %s)", drive.drive_id, mode)
            return None, None

    def _resolve_prefilled_form_url(self, drive, profile, resume_file: ResumeFile | None) -> str | None:
        """Best-effort re-attempt at form autofill now that the resume is
        finally known — mirrors run.py's own attempt, but never blocks: a
        failure here just means the final card shows a plain (not
        pre-filled) form link, same as when autofill isn't configured at all."""
        if not drive.form_url or self._settings is None:
            return None
        try:
            from cutoff.pipeline import form_autofill, ingest

            resume_text = ""
            if resume_file is not None and self._adapters.files is not None:
                ok, data, err = executor.with_retry(lambda: self._adapters.files.get_resume_content(resume_file.file_id))
                if ok:
                    try:
                        resume_text = ingest.extract_pdf_text(data)
                    except Exception:
                        resume_text = ""
            autofilled = form_autofill.build_autofilled_url(
                drive.form_url, profile, resume_text, drive.jd_text or "",
                resume_link=(resume_file.web_view_link if resume_file else None),
                api_key=self._settings.llm_api_key, model=self._settings.llm_model,
                provider=self._settings.llm_provider, base_url=self._settings.llm_base_url,
            )
            if autofilled:
                return autofilled
        except Exception:
            logger.exception("form autofill retry failed after resume choice for drive %s", drive.drive_id)

        if self._adapters.sheets is not None:
            try:
                from cutoff.pipeline import form_prefill

                template = self._adapters.sheets.read_form_template(drive.form_url)
                return form_prefill.build_prefilled_url(template, profile)
            except Exception:
                return None
        return None

    def _new_epoch_for_action(self, action_id: str) -> bool:
        """Recovers which idempotency key the resume-choice card was
        originally planned under (":main" vs ":v{version}") so
        plan_resolved_approval edits that SAME message rather than sending a
        new one — see plan_resolved_approval's own docstring."""
        conn = db.get_connection(self._db_path)
        row = conn.execute("SELECT idempotency_key FROM actions WHERE action_id = ?", (action_id,)).fetchone()
        return bool(row) and not row["idempotency_key"].endswith(":main")

    # --- question flow (Yes / No / Show email) -------------------------------

    def _handle_question(self, approval: dict, action: str) -> None:
        if approval["status"] != "PENDING":
            return

        if action == "yes":
            self._void_approval(approval["approval_id"])
            self._send_mark_submitted_prompt(approval)
        elif action == "no":
            self._void_approval(approval["approval_id"])
            self._edit_original(approval, "Got it — marked as not eligible. No action taken.")
        elif action == "show":
            self._edit_original(
                approval,
                "Check the original email in your inbox for the exact wording — "
                "CutOff doesn't keep a copy of email bodies after extraction.",
            )

    # --- Mark submitted (the one T2 action, Section 11.2) --------------------

    def handle_mark_submitted(self, token: str) -> None:
        """Called for callback_data 'm:<token>:submitted' — kept as a public
        method since it needs a dispatch entry in `_handle_callback` too."""
        approval = self._get_approval(token)
        if approval is None or approval["status"] != "PENDING":
            return
        drive = resolve.get_drive(self._db_path, approval["drive_id"])
        if drive is None:
            return
        drive.registered = True
        resolve.save_drive(self._db_path, drive)
        self._void_approval(token)

        row_action = Action(
            action_id=uuid.uuid4().hex, idempotency_key=f"sheets:{drive.drive_id}:row",
            drive_id=drive.drive_id, drive_version=drive.version, app="sheets", kind="upsert_row",
            tier="T2_NEEDS_HUMAN", payload={"row": {"drive_id": drive.drive_id, "status": "REGISTERED"}},
        )
        executor.plan_action(self._db_path, row_action)
        executor.run_pending(self._db_path, self._adapters)
        self._edit_original(approval, f"Marked as registered for {drive.company} ({drive.role}). Good luck!")

    # --- shared helpers ------------------------------------------------------

    def _void_approval(self, approval_id: str) -> None:
        conn = db.get_connection(self._db_path)
        conn.execute(
            "UPDATE approvals SET status = 'VOIDED', decided_at = ? WHERE approval_id = ?",
            (datetime.now(timezone.utc).isoformat(), approval_id),
        )
        conn.commit()

    def _edit_original(self, approval: dict, text: str) -> None:
        message_id = approval.get("telegram_message_id")
        if message_id:
            self._adapters.messenger.edit(message_id, text, None)

    def _plan_and_run_message(self, drive, *, key: str, text: str, buttons: list[dict]) -> Action:
        action = Action(
            action_id=uuid.uuid4().hex, idempotency_key=key, drive_id=drive.drive_id,
            drive_version=drive.version, app="telegram", kind="send_msg", tier="T1_REVERSIBLE",
            payload={"text": text, "buttons": buttons},
        )
        planned = executor.plan_action(self._db_path, action)
        executor.run_pending(self._db_path, self._adapters)
        return planned

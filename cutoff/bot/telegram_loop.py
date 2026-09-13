"""Long-polling Telegram bot loop (Section 6.5): getUpdates -> callback_query
dispatch, plus (Section 6.4 onboarding extension) incoming message handling
for /onboard, /sync, and resume-PDF uploads. Only accepts callbacks and
messages from TELEGRAM_CHAT_ID. Registration is the one T2 action (Section
11.2) — the agent never submits the form; tapping "Mark submitted" is the
human's own confirmation that they did."""
from __future__ import annotations

import json
import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

from cutoff import db, trace
from cutoff.models import Action, Button, ResumeFile
from cutoff.pipeline import executor, planner, resolve
from cutoff.pipeline import resume as resume_mod
from cutoff.pipeline.executor import Adapters

logger = logging.getLogger("cutoff.bot")

POLL_TIMEOUT = 25
MAX_DOCUMENT_BYTES = 5 * 1024 * 1024  # a resume PDF is realistically well under this

_ONBOARD_HELP = (
    "Send me your existing resume as a PDF and I'll extract your skills, projects, education, and "
    "achievements into your master profile (used for tailored resume generation).\n\n"
    "You can also run:\n"
    "/sync <github_username> [leetcode_username]\n"
    "to pull in your public GitHub repos and/or LeetCode stats without a new resume upload.\n\n"
    "Nothing is saved until you approve the preview I send back."
)


class TelegramBotLoop:
    def __init__(self, bot_token: str, chat_id: str, db_path: str, adapters: Adapters, *, settings=None):
        self._bot_token = bot_token
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
                continue
            message = update.get("message")
            if message:
                self._handle_message(message)

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
            if kind == "o":
                self._handle_onboard_callback(token, action)
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

    # --- onboarding (/onboard, /sync, resume upload) --------------------------
    # Section 6.4 onboarding extension: builds config/master_profile.yaml from
    # a resume the student already has (cutoff.llm.profile_extract), plus
    # optional public GitHub/LeetCode enrichment
    # (cutoff.adapters.developer_footprint), merged deterministically
    # (cutoff.pipeline.profile_synthesize) — but NEVER written until the
    # student explicitly taps "Approve & Save" on a preview. A bug anywhere
    # in this whole flow must never take down message polling itself, same
    # discipline as resend_due_reminders — every entry point below is
    # wrapped in a broad try/except that degrades to an honest chat message.

    def _handle_message(self, message: dict) -> None:
        chat_id = str(message.get("chat", {}).get("id", ""))
        if chat_id != self._chat_id:
            return  # Section 6.5: only TELEGRAM_CHAT_ID is trusted, same as callbacks
        try:
            text = (message.get("text") or "").strip()
            if text == "/onboard" or text.startswith("/onboard "):
                self._send_plain(_ONBOARD_HELP)
            elif text.startswith("/sync"):
                self._handle_sync_command(text)
            elif message.get("document"):
                self._handle_resume_upload(message["document"])
        except Exception:
            logger.exception("handling an incoming Telegram message failed")
            self._send_plain("Something went wrong handling that — nothing was changed.")

    def _handle_sync_command(self, text: str) -> None:
        if self._settings is None:
            self._send_plain("Profile onboarding isn't configured on this instance.")
            return
        parts = text.split()[1:]
        if not parts:
            self._send_plain("Usage: /sync <github_username> [leetcode_username]")
            return

        from cutoff.adapters.developer_footprint import fetch_github_profile, fetch_leetcode_stats

        github_username, leetcode_username = parts[0], (parts[1] if len(parts) > 1 else None)
        github_data = fetch_github_profile(github_username)
        leetcode_data = fetch_leetcode_stats(leetcode_username) if leetcode_username else None
        if not github_data["repos"] and leetcode_data is None:
            self._send_plain(
                f"Couldn't find anything public for {github_username!r}"
                + (f" / {leetcode_username!r}" if leetcode_username else "") + " — nothing was changed."
            )
            return
        self._stage_and_send_preview(github_data=github_data, leetcode_data=leetcode_data)

    def _handle_resume_upload(self, document: dict) -> None:
        if self._settings is None:
            self._send_plain("Profile onboarding isn't configured on this instance.")
            return
        if (document.get("file_size") or 0) > MAX_DOCUMENT_BYTES:
            self._send_plain("That file's too large (max 5MB) — please send a smaller PDF.")
            return
        if not (document.get("file_name") or "").lower().endswith(".pdf"):
            self._send_plain("Please send your resume as a PDF.")
            return

        from cutoff.pipeline.ingest import extract_pdf_hyperlinks, extract_pdf_text
        from cutoff.llm.profile_extract import extract_profile_from_resume_text

        pdf_bytes = self._download_file(document["file_id"])
        resume_text = extract_pdf_text(pdf_bytes)
        if not resume_text.strip():
            self._send_plain("Couldn't read any text from that PDF — is it a scanned image?")
            return
        hyperlinks = extract_pdf_hyperlinks(pdf_bytes)

        parsed = extract_profile_from_resume_text(
            resume_text, api_key=self._settings.llm_api_key, model=self._settings.llm_model,
            provider=self._settings.llm_provider, base_url=self._settings.llm_base_url, db_path=self._db_path,
            hyperlinks=hyperlinks,
        )
        self._stage_and_send_preview(parsed_resume=parsed)

    def _download_file(self, file_id: str) -> bytes:
        resp = httpx.get(f"{self._base}/getFile", params={"file_id": file_id}, timeout=25)
        resp.raise_for_status()
        file_path = resp.json()["result"]["file_path"]
        file_resp = httpx.get(f"https://api.telegram.org/file/bot{self._bot_token}/{file_path}", timeout=25)
        file_resp.raise_for_status()
        return file_resp.content

    def _stage_and_send_preview(self, *, parsed_resume=None, github_data=None, leetcode_data=None) -> None:
        from cutoff.pipeline.master_profile import generate_profile_diff_summary, load_master_profile, save_master_profile
        from cutoff.pipeline.profile_synthesize import merge_master_profile

        target_path = Path(self._settings.master_profile_path)
        existing = load_master_profile(target_path)
        merged = merge_master_profile(
            existing=existing, parsed_resume=parsed_resume, github_data=github_data, leetcode_data=leetcode_data,
        )
        diff_summary = generate_profile_diff_summary(existing, merged)
        if diff_summary == "No changes.":
            self._send_plain("No new information found — your master profile is already up to date.")
            return

        token = executor.new_short_token()
        staged_path = target_path.parent / f".staged_{token}.yaml"
        save_master_profile(merged, staged_path)
        conn = db.get_connection(self._db_path)
        conn.execute(
            "INSERT INTO profile_imports (token, staged_path, diff_summary, created_at) VALUES (?, ?, ?, ?)",
            (token, str(staged_path), diff_summary, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

        text = f"Here's what I found:\n\n{diff_summary}\n\nSave this to your master profile?"
        buttons = [Button(text="Approve & Save", callback_data=f"o:{token}:approve"),
                   Button(text="Discard", callback_data=f"o:{token}:discard")]
        self._adapters.messenger.send(f"onboard:{token}", text, buttons)

    def _handle_onboard_callback(self, token: str, action: str) -> None:
        conn = db.get_connection(self._db_path)
        row = conn.execute("SELECT * FROM profile_imports WHERE token = ?", (token,)).fetchone()
        if row is None:
            return  # already resolved (double tap) or an unknown/expired token
        staged_path = Path(row["staged_path"])
        conn.execute("DELETE FROM profile_imports WHERE token = ?", (token,))
        conn.commit()

        if action == "approve" and self._settings is not None and staged_path.exists():
            from cutoff.pipeline.master_profile import backup_master_profile

            target_path = Path(self._settings.master_profile_path)
            backup_path = backup_master_profile(target_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged_path), str(target_path))
            note = f" (previous version backed up to {backup_path.name})" if backup_path else ""
            self._send_plain(f"Master profile updated!{note} Ready for tailored resume generation.")
        else:
            if staged_path.exists():
                staged_path.unlink()
            self._send_plain("Discarded — your master profile wasn't changed.")

    def _send_plain(self, text: str) -> None:
        self._adapters.messenger.send("onboard:info", text, None)

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

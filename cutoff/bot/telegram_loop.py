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
from cutoff.models import Action
from cutoff.pipeline import executor, resolve
from cutoff.pipeline.executor import Adapters

logger = logging.getLogger("cutoff.bot")

POLL_TIMEOUT = 25


class TelegramBotLoop:
    def __init__(self, bot_token: str, chat_id: str, db_path: str, adapters: Adapters):
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._chat_id = str(chat_id)
        self._db_path = db_path
        self._adapters = adapters
        self._offset: int | None = None

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
            buttons = [
                {**b, "callback_data": b["callback_data"].replace(f"a:{old_token}:", f"a:{new_token}:")}
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

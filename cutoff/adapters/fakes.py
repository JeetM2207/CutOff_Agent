"""In-memory fake adapters for all five apps (Section 6). Every call is recorded
in `.calls` (method name + args, in order) so the eval harness can check for
duplicate or forbidden effects against the app's final state, not the agent's
self-report (Section 15.1)."""
from __future__ import annotations

import uuid
from datetime import datetime

from cutoff.models import (
    Button,
    CalendarEvent,
    CollegePolicy,
    EmailMessage,
    EmailRef,
    ResumeFile,
    StudentProfile,
)


class CallRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def _record(self, method: str, **kwargs) -> None:
        self.calls.append((method, kwargs))


class FakeMailSource(CallRecorder):
    def __init__(self, messages: list[EmailMessage] | None = None) -> None:
        super().__init__()
        self._messages: dict[str, EmailMessage] = {}
        self._order: list[str] = []
        self._attachments: dict[tuple[str, str], bytes] = {}
        for m in messages or []:
            self.deliver(m)

    def deliver(self, msg: EmailMessage, attachments: dict[str, bytes] | None = None) -> None:
        """Simulate new mail arriving, including a duplicate `message_id` delivery."""
        self._messages[msg.message_id] = msg
        if msg.message_id not in self._order:
            self._order.append(msg.message_id)
        for att_id, data in (attachments or {}).items():
            self._attachments[(msg.message_id, att_id)] = data

    def list_new(self, senders: list[str], since: datetime) -> list[EmailRef]:
        self._record("list_new", senders=senders, since=since)
        allowed = {s.lower() for s in senders}
        out = []
        for mid in self._order:
            msg = self._messages[mid]
            sender_email = _extract_email(msg.from_addr).lower()
            if allowed and sender_email not in allowed:
                continue
            if msg.received_at < since:
                continue
            out.append(EmailRef(
                message_id=msg.message_id, thread_id=msg.thread_id,
                from_addr=msg.from_addr, subject=msg.subject,
                received_at=msg.received_at,
            ))
        return out

    def list_suspicious(self, keywords: list[str], since: datetime) -> list[EmailRef]:
        self._record("list_suspicious", keywords=keywords, since=since)
        needles = [k.lower() for k in keywords]
        out = []
        for mid in self._order:
            msg = self._messages[mid]
            if msg.received_at < since:
                continue
            haystack = f"{msg.subject}\n{msg.body_text}".lower()
            if any(k in haystack for k in needles):
                out.append(EmailRef(
                    message_id=msg.message_id, thread_id=msg.thread_id,
                    from_addr=msg.from_addr, subject=msg.subject,
                    received_at=msg.received_at,
                ))
        return out

    def get(self, message_id: str) -> EmailMessage:
        self._record("get", message_id=message_id)
        return self._messages[message_id]

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        self._record("get_attachment", message_id=message_id, attachment_id=attachment_id)
        return self._attachments[(message_id, attachment_id)]


def _extract_email(from_addr: str) -> str:
    if "<" in from_addr and ">" in from_addr:
        return from_addr.split("<", 1)[1].split(">", 1)[0].strip()
    return from_addr.strip()


class FakeSheetStore(CallRecorder):
    def __init__(
        self, profile: StudentProfile, policy: CollegePolicy, form_templates: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self._profile = profile
        self._policy = policy
        self._rows: dict[str, dict] = {}
        self._form_templates = form_templates or {}

    def read_profile(self) -> StudentProfile:
        self._record("read_profile")
        return self._profile

    def read_policy(self) -> CollegePolicy:
        self._record("read_policy")
        return self._policy

    def upsert_drive_row(self, drive_id: str, row: dict) -> None:
        self._record("upsert_drive_row", drive_id=drive_id, row=row)
        self._rows[drive_id] = {**self._rows.get(drive_id, {}), **row}

    def read_drive_row(self, drive_id: str) -> dict | None:
        self._record("read_drive_row", drive_id=drive_id)
        return self._rows.get(drive_id)

    def read_form_template(self, form_url: str) -> str | None:
        self._record("read_form_template", form_url=form_url)
        return self._form_templates.get(form_url)


class FakeCalendarStore(CallRecorder):
    def __init__(self, exam_events: list[CalendarEvent] | None = None) -> None:
        super().__init__()
        self._events: dict[str, CalendarEvent] = {}
        self._exam_events: list[CalendarEvent] = exam_events or []

    def upsert_event(self, event_id: str, event: CalendarEvent) -> str:
        self._record("upsert_event", event_id=event_id, event=event)
        stored = event.model_copy(update={"event_id": event_id, "status": "confirmed"})
        self._events[event_id] = stored
        return event_id

    def cancel_event(self, event_id: str) -> None:
        self._record("cancel_event", event_id=event_id)
        existing = self._events.get(event_id)
        if existing is not None:
            self._events[event_id] = existing.model_copy(update={"status": "cancelled"})

    def get_event(self, event_id: str) -> CalendarEvent | None:
        self._record("get_event", event_id=event_id)
        return self._events.get(event_id)

    def list_exam_events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        self._record("list_exam_events", start=start, end=end)
        return [e for e in self._exam_events if e.start < end and (e.end or e.start) > start]


class FakeFileStore(CallRecorder):
    def __init__(self, resumes: list[ResumeFile] | None = None, contents: dict[str, bytes] | None = None) -> None:
        super().__init__()
        self._resumes = resumes or []
        self._contents = contents or {}

    def list_resumes(self) -> list[ResumeFile]:
        self._record("list_resumes")
        return list(self._resumes)

    def get_resume_content(self, file_id: str) -> bytes:
        self._record("get_resume_content", file_id=file_id)
        return self._contents.get(file_id, b"")


class FakeMessenger(CallRecorder):
    def __init__(self) -> None:
        super().__init__()
        self._messages: dict[str, dict] = {}

    def send(self, key: str, text: str, buttons: list[Button] | None) -> str:
        self._record("send", key=key, text=text, buttons=buttons)
        message_id = uuid.uuid4().hex[:10]
        self._messages[message_id] = {"key": key, "text": text, "buttons": buttons}
        return message_id

    def edit(self, message_id: str, text: str, buttons: list[Button] | None) -> None:
        self._record("edit", message_id=message_id, text=text, buttons=buttons)
        if message_id in self._messages:
            self._messages[message_id].update(text=text, buttons=buttons)

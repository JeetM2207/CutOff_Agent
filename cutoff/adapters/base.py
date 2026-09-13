"""Protocols for every external app (Section 6). The pipeline only ever depends
on these; real/fake/arga implementations are swapped in by cutoff.config.MODE."""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from cutoff.models import (
    Button,
    CalendarEvent,
    CollegePolicy,
    EmailMessage,
    EmailRef,
    ResumeFile,
    StudentProfile,
)


class MailSource(Protocol):
    def list_new(self, senders: list[str], since: datetime) -> list[EmailRef]: ...

    def get(self, message_id: str) -> EmailMessage: ...  # body text + attachment metadata

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes: ...

    def list_suspicious(self, keywords: list[str], since: datetime) -> list[EmailRef]:
        """Section 6.1/12's "second, broader query": recruiting-keyword-scoped,
        with NO sender restriction — the only way a lookalike-domain or
        spoofed sender ever gets fetched at all, since list_new above is
        deliberately narrowed to the allowlist. Found live: security.py's
        own check_lookalike/check_display_spoof existed all along but could
        never fire in real polling, because the message never reached them.
        Scoped by keyword (not "everyone") specifically so it stays safe
        against a real, high-volume inbox — see the spec's own note on why
        an unscoped broader query "starves the loop"."""
        ...


class SheetStore(Protocol):
    def read_profile(self) -> StudentProfile: ...

    def read_policy(self) -> CollegePolicy: ...

    def upsert_drive_row(self, drive_id: str, row: dict) -> None: ...  # keyed by drive_id in column A

    def read_drive_row(self, drive_id: str) -> dict | None: ...

    def read_form_template(self, form_url: str) -> str | None: ...  # raw pre-filled link, placeholder tokens as values


class CalendarStore(Protocol):
    def upsert_event(self, event_id: str, event: CalendarEvent) -> str: ...  # idempotent by event_id

    def cancel_event(self, event_id: str) -> None: ...

    def get_event(self, event_id: str) -> CalendarEvent | None: ...

    def list_exam_events(self, start: datetime, end: datetime) -> list[CalendarEvent]: ...


class FileStore(Protocol):
    def list_resumes(self) -> list[ResumeFile]: ...  # name, file_id, web_view_link

    def get_resume_content(self, file_id: str) -> bytes: ...  # raw PDF bytes, for JD-content matching


class Messenger(Protocol):
    def send(self, key: str, text: str, buttons: list[Button] | None) -> str: ...  # returns message_id

    def edit(self, message_id: str, text: str, buttons: list[Button] | None) -> None: ...

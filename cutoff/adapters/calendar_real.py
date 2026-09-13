"""Real Google Calendar adapter (Section 6.3). Client-chosen, deterministic
event IDs (executor.event_id_for) make every write idempotent: insert; a 409
just means this event_id already exists (e.g. a REVISION re-upserting the
same deadline reminder), so patch it in place instead. Found live (Phase 5+):
the previous version raised CalendarConflict for the executor to "get, then
retry insert" — but retrying insert on an id that already exists just 409s
again, uncaught this time, crashing the whole run_pending loop every poll.
Self-healing here means upsert_event is actually idempotent on its own."""
from __future__ import annotations

from datetime import datetime

from googleapiclient.errors import HttpError

from cutoff.adapters.google_auth import raise_as_adapter_error
from cutoff.models import CalendarEvent


class CalendarSource:
    def __init__(self, service, calendar_id: str, exam_calendar_id: str | None = None):
        self._service = service
        self._calendar_id = calendar_id
        self._exam_calendar_id = exam_calendar_id

    def upsert_event(self, event_id: str, event: CalendarEvent) -> str:
        body = _to_google_event(event_id, event)
        try:
            self._service.events().insert(calendarId=self._calendar_id, body=body).execute()
        except HttpError as e:
            if e.resp.status != 409:
                raise_as_adapter_error(e)
            try:
                self._service.events().patch(
                    calendarId=self._calendar_id, eventId=event_id, body=body,
                ).execute()
            except HttpError as e2:
                raise_as_adapter_error(e2)
        return event_id

    def cancel_event(self, event_id: str) -> None:
        try:
            self._service.events().patch(
                calendarId=self._calendar_id, eventId=event_id, body={"status": "cancelled"},
            ).execute()
        except HttpError as e:
            if e.resp.status == 404:
                return  # already gone: cancelling a cancelled/missing event is a no-op
            raise_as_adapter_error(e)

    def get_event(self, event_id: str) -> CalendarEvent | None:
        try:
            g = self._service.events().get(calendarId=self._calendar_id, eventId=event_id).execute()
        except HttpError as e:
            if e.resp.status == 404:
                return None
            raise_as_adapter_error(e)
        return _from_google_event(g)

    def list_exam_events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        if not self._exam_calendar_id:
            return []
        try:
            resp = self._service.events().list(
                calendarId=self._exam_calendar_id, timeMin=start.isoformat(), timeMax=end.isoformat(),
                singleEvents=True,
            ).execute()
        except HttpError as e:
            raise_as_adapter_error(e)
        return [_from_google_event(g) for g in resp.get("items", [])]


def _to_google_event(event_id: str, event: CalendarEvent) -> dict:
    body = {
        "id": event_id,
        "summary": event.title,
        "start": {"dateTime": event.start.isoformat()},
        "end": {"dateTime": (event.end or event.start).isoformat()},
        "status": event.status,
    }
    if event.description:
        body["description"] = event.description
    if event.location:
        body["location"] = event.location
    return body


def _from_google_event(g: dict) -> CalendarEvent:
    start_raw = g.get("start", {}).get("dateTime") or g.get("start", {}).get("date")
    end_block = g.get("end") or {}
    end_raw = end_block.get("dateTime") or end_block.get("date")
    return CalendarEvent(
        event_id=g["id"], title=g.get("summary", ""),
        start=datetime.fromisoformat(start_raw), end=datetime.fromisoformat(end_raw) if end_raw else None,
        description=g.get("description"), location=g.get("location"),
        status=g.get("status", "confirmed"),
    )

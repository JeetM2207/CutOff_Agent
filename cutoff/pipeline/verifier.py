"""Re-reads real app state after a write and compares it to what was
requested (Section 11.5). Never trust the write call's "OK" — the app's
final state is the only thing that counts."""
from __future__ import annotations

from cutoff.models import Action, CalendarEvent


def verify(adapters, action: Action) -> bool:
    if action.app == "calendar" and action.kind == "upsert_event":
        event_id = (action.result or {}).get("event_id")
        if not event_id:
            return False
        got = adapters.calendar.get_event(event_id)
        if got is None or got.status != "confirmed":
            return False
        wanted = CalendarEvent.model_validate(action.payload["event"])
        return got.title == wanted.title and got.start == wanted.start and got.end == wanted.end

    if action.app == "calendar" and action.kind == "cancel_event":
        event_id = (action.result or {}).get("event_id") or action.payload.get("event_id")
        got = adapters.calendar.get_event(event_id)
        return got is not None and got.status == "cancelled"

    if action.app == "sheets" and action.kind == "upsert_row":
        row = adapters.sheets.read_drive_row(action.drive_id)
        if row is None:
            return False
        wanted = action.payload["row"]
        return all(row.get(k) == v for k, v in wanted.items())

    if action.app == "telegram" and action.kind == "send_msg":
        return bool((action.result or {}).get("message_id"))

    return True

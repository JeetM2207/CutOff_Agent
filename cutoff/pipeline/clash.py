"""Exam-clash detection (Section 11.3): does a drive's test/interview slot
overlap anything on the student's exam calendar?"""
from __future__ import annotations

from cutoff.adapters.base import CalendarStore
from cutoff.models import CalendarEvent, DriveEvent


def find_clashes(calendar: CalendarStore, event: DriveEvent) -> list[CalendarEvent]:
    window_end = event.end or event.start
    exams = calendar.list_exam_events(event.start, window_end)
    return [e for e in exams if e.start < window_end and (e.end or e.start) > event.start]


def clash_warning(clashes: list[CalendarEvent]) -> str | None:
    if not clashes:
        return None
    names = ", ".join(f"{e.title} ({e.start.isoformat()})" for e in clashes)
    return f"Clashes with your exam schedule: {names}."

"""Section 11.3: exam-clash detection for scheduled test/interview slots."""
from datetime import datetime, timezone

from cutoff.adapters.fakes import FakeCalendarStore
from cutoff.models import CalendarEvent, DriveEvent
from cutoff.pipeline.clash import clash_warning, find_clashes


def exam(start, end, title="DBMS Midterm") -> CalendarEvent:
    return CalendarEvent(event_id="exam1", title=title, start=start, end=end)


def test_overlapping_slot_is_a_clash():
    calendar = FakeCalendarStore(exam_events=[
        exam(datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc), datetime(2026, 9, 12, 11, 0, tzinfo=timezone.utc)),
    ])
    ev = DriveEvent(kind="TEST", start=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
                     end=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc))
    clashes = find_clashes(calendar, ev)
    assert len(clashes) == 1
    assert "DBMS Midterm" in clash_warning(clashes)


def test_non_overlapping_slot_is_not_a_clash():
    calendar = FakeCalendarStore(exam_events=[
        exam(datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc), datetime(2026, 9, 12, 11, 0, tzinfo=timezone.utc)),
    ])
    ev = DriveEvent(kind="TEST", start=datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc),
                     end=datetime(2026, 9, 12, 16, 0, tzinfo=timezone.utc))
    assert find_clashes(calendar, ev) == []
    assert clash_warning([]) is None


def test_no_exam_calendar_seeded_means_no_clashes():
    calendar = FakeCalendarStore()
    ev = DriveEvent(kind="TEST", start=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
                     end=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc))
    assert find_clashes(calendar, ev) == []


def test_point_in_time_event_with_no_end_uses_start_as_window():
    calendar = FakeCalendarStore(exam_events=[
        exam(datetime(2026, 9, 12, 9, 30, tzinfo=timezone.utc), datetime(2026, 9, 12, 11, 0, tzinfo=timezone.utc)),
    ])
    ev = DriveEvent(kind="INTERVIEW", start=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc), end=None)
    clashes = find_clashes(calendar, ev)
    assert len(clashes) == 1

"""timeparse.resolve_deadline (Section 8 step 4). Regression test for a live
bug: dateparser's own abbreviation table treats "IST" as ambiguous (India /
Israel / Irish Standard Time) and can silently resolve it to the wrong one
even with an explicit TIMEZONE setting — "11:59 PM IST" parsed 3.5 hours off
from the identical string without "IST". Since this project's deadlines are
always Indian campus recruiting deadlines (Asia/Kolkata by default), the fix
strips the abbreviation rather than trusting dateparser's disambiguation."""
from datetime import datetime, timezone

from cutoff.pipeline import timeparse

RECEIVED_AT = datetime(2026, 9, 11, 17, 45, tzinfo=timezone.utc)


def test_ist_suffix_resolves_to_india_standard_time_not_a_different_zone():
    with_ist = timeparse.resolve_deadline("25th September 2026, 11:59 PM IST.", RECEIVED_AT, "Asia/Kolkata")
    without_ist = timeparse.resolve_deadline("25th September 2026, 11:59 PM", RECEIVED_AT, "Asia/Kolkata")

    assert with_ist == without_ist
    # 23:59 IST (+05:30) on Sept 25 is 18:29 UTC — not 21:59 UTC (Sept 25's
    # 23:59 misread as +02:00) and not the next calendar day.
    assert with_ist == datetime(2026, 9, 25, 18, 29, tzinfo=timezone.utc)


def test_ist_suffix_lowercase_and_no_trailing_period():
    parsed = timeparse.resolve_deadline("25th September 2026, 11:59 PM ist", RECEIVED_AT, "Asia/Kolkata")
    assert parsed == datetime(2026, 9, 25, 18, 29, tzinfo=timezone.utc)


def test_relative_deadline_still_resolves_against_received_at():
    parsed = timeparse.resolve_deadline("by tomorrow, 11:59 PM", RECEIVED_AT, "Asia/Kolkata")
    # received_at is 2026-09-11 23:15 IST, so "tomorrow" is the 12th.
    assert parsed == datetime(2026, 9, 12, 18, 29, tzinfo=timezone.utc)


def test_none_and_empty_text_return_none():
    assert timeparse.resolve_deadline(None, RECEIVED_AT) is None
    assert timeparse.resolve_deadline("", RECEIVED_AT) is None
    assert timeparse.resolve_deadline("   ", RECEIVED_AT) is None


def test_deadlines_agree_within_tolerance():
    a = datetime(2026, 9, 25, 18, 29, tzinfo=timezone.utc)
    b = datetime(2026, 9, 25, 18, 29, 30, tzinfo=timezone.utc)  # 30s apart
    assert timeparse.deadlines_agree(a, b) is True


def test_deadlines_disagree_beyond_tolerance():
    a = datetime(2026, 9, 25, 18, 29, tzinfo=timezone.utc)
    b = datetime(2026, 9, 25, 21, 59, tzinfo=timezone.utc)  # the old IST-bug gap
    assert timeparse.deadlines_agree(a, b) is False

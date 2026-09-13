"""Relative-date resolution for deadlines (Section 8, step 4: Dates).

Wording like "tonight" or "tomorrow EOD" must resolve against the email's
`received_at`, never against wall-clock `now`.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import dateparser

# Found live: "11:59 PM IST" parsed 3.5 hours off from the identical string
# without "IST" — dateparser's own abbreviation table treats "IST" as
# ambiguous (India / Israel / Irish Standard Time) and can silently resolve
# it to the wrong one, even with an explicit TIMEZONE setting: an explicit
# abbreviation found in the text takes priority over that setting. Since
# `tz_name` already encodes the intended zone (Asia/Kolkata by default —
# this project targets Indian campus recruiting, where "IST" in a deadline
# always means India Standard Time), stripping the abbreviation rather than
# trusting dateparser's disambiguation is the reliable fix.
_IST_RE = re.compile(r"\bIST\b", re.IGNORECASE)


def resolve_deadline(deadline_text: str | None, received_at: datetime, tz_name: str = "Asia/Kolkata") -> datetime | None:
    """Parse `deadline_text` relative to `received_at` in `tz_name`. Returns UTC, or None if unparseable."""
    if not deadline_text or not deadline_text.strip():
        return None

    tz = ZoneInfo(tz_name)
    local_received = received_at.astimezone(tz).replace(tzinfo=None)
    cleaned_text = _IST_RE.sub("", deadline_text).strip()

    parsed = dateparser.parse(
        cleaned_text,
        settings={
            "TIMEZONE": tz_name,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "RELATIVE_BASE": local_received,
            "PREFER_DATES_FROM": "future",
        },
    )
    if parsed is None:
        return None
    return parsed.astimezone(timezone.utc)


def deadlines_agree(a: datetime | None, b: datetime | None, tolerance_minutes: int = 1) -> bool:
    """Do the LLM's `deadline` and the re-parsed `deadline_text` agree within tolerance?"""
    if a is None or b is None:
        return a is None and b is None
    return abs((a - b).total_seconds()) <= tolerance_minutes * 60


def deadline_state(deadline: datetime | None, now: datetime, closing_soon_hours: int = 6) -> str:
    """OPEN, CLOSING_SOON (< closing_soon_hours), PASSED, or UNKNOWN (no deadline)."""
    if deadline is None:
        return "UNKNOWN"
    remaining = deadline - now
    if remaining.total_seconds() <= 0:
        return "PASSED"
    if remaining <= timedelta(hours=closing_soon_hours):
        return "CLOSING_SOON"
    return "OPEN"

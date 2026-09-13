"""resolve_drive's company fuzzy-match (Section 8, step 5) — a short/partial
company reference must trigger the "which drive do you mean?" question
rather than either silently guessing or falling through to a generic
fallback. Found live (dev_034_ambiguous_two_drive_resolution): plain
fuzz.ratio scored "Solstice" at only ~57% against "Solstice Innovations" —
below the review band — because ratio penalizes a short string purely for
being shorter than the full name it's actually naming. Switched to
partial_ratio, which finds the best-aligned substring match instead."""
from datetime import datetime, timezone

from cutoff import db
from cutoff.models import Criteria, Drive, Notice
from cutoff.pipeline import resolve

NOW = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)


def _open_drive(drive_id: str, company: str, role: str) -> Drive:
    return Drive(
        drive_id=drive_id, company=company, role=role, status="OPEN",
        criteria=Criteria(min_gpa=7.0), last_verdict="ELIGIBLE",
    )


def test_short_partial_company_name_asks_which_of_two_similar_drives(tmp_path):
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)
    resolve.save_drive(db_path, _open_drive(
        "solstice-innovations:software-engineer:2026", "Solstice Innovations", "Software Engineer"
    ))
    resolve.save_drive(db_path, _open_drive(
        "solstice-industries:mechanical-engineer:2026", "Solstice Industries", "Mechanical Engineer"
    ))

    notice = Notice(notice_type="SCHEDULE", company="Solstice")
    result = resolve.resolve_drive(db_path, thread_id="t3", notice=notice, batch_season="2026")

    assert result.drive is None  # never silently attaches to either
    assert result.method == "fuzzy_ambiguous"
    assert "Solstice Innovations" in result.needs_review_question
    assert "Solstice Industries" in result.needs_review_question


def test_short_partial_company_name_still_auto_resolves_when_only_one_candidate_exists(tmp_path):
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)
    resolve.save_drive(db_path, _open_drive(
        "solstice-innovations:software-engineer:2026", "Solstice Innovations", "Software Engineer"
    ))

    notice = Notice(notice_type="SCHEDULE", company="Solstice")
    result = resolve.resolve_drive(db_path, thread_id="t3", notice=notice, batch_season="2026")

    assert result.method == "fuzzy"
    assert result.drive.drive_id == "solstice-innovations:software-engineer:2026"


def test_two_genuinely_different_full_company_names_are_not_confused(tmp_path):
    """Guards against partial_ratio being too permissive: two full,
    merely similar-sounding company names sharing a first word must not
    get treated as the same drive or as ambiguous with each other."""
    db_path = str(tmp_path / "cutoff.db")
    db.init_db(db_path)
    resolve.save_drive(db_path, _open_drive(
        "zentrix-analytics:data-analyst:2026", "Zentrix Analytics", "Data Analyst"
    ))

    notice = Notice(notice_type="NEW_DRIVE", company="Zentrix Robotics", role="Robotics Engineer")
    result = resolve.resolve_drive(db_path, thread_id="t9", notice=notice, batch_season="2026")

    # A genuinely new, different company: resolves as "new", not fuzzy-matched
    # onto the unrelated existing "Zentrix Analytics" drive.
    assert result.method == "new"
    assert result.drive is None

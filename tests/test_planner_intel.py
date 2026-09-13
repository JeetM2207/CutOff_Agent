"""Interview-prep intel on the approval card (new extension):
planner._answer_card_text's formatting when drive.prep_intel is present,
absent, or "attempted but found nothing." No LLM/network involved —
_answer_card_text is a pure function of its Drive/profile/resume args."""
from datetime import datetime, timezone

from cutoff.models import Criteria, Drive, ResumeFile, StudentProfile
from cutoff.pipeline import planner

NOW = datetime(2026, 9, 10, 20, 0, tzinfo=timezone.utc)


def _drive(**overrides) -> Drive:
    base = dict(
        drive_id="zentrix:sde:2026", company="Zentrix Analytics", role="Software Engineer",
        version=1, criteria=Criteria(min_gpa=7.0), deadline=NOW,
    )
    base.update(overrides)
    return Drive(**base)


def _profile() -> StudentProfile:
    return StudentProfile(
        name="Riya Mehta", roll_no="21BCS045", email="riya@college.edu", branch="CSE",
        gpa=7.42, active_backlogs=0, batch_year=2026,
    )


def test_answer_card_includes_prep_intel_when_present():
    drive = _drive(prep_intel={
        "attempted": True,
        "strategy_summary": "Historically, Zentrix focuses heavily on Graph traversals and SQL window functions.",
        "top_reference_links": ["https://leetcode.com/discuss/12345", "https://www.geeksforgeeks.org/zentrix-oa"],
    })

    text = planner._answer_card_text(drive, _profile(), None, None, None)

    assert "💡 Prep Strategy: Historically, Zentrix focuses heavily on Graph traversals" in text
    assert "🔗 Study materials: https://leetcode.com/discuss/12345 | https://www.geeksforgeeks.org/zentrix-oa" in text
    # the prep-intel block comes after the form line
    assert text.index("Form:") < text.index("Prep Strategy")


def test_answer_card_shows_a_single_reference_link_without_a_separator():
    drive = _drive(prep_intel={
        "attempted": True, "strategy_summary": "Expect a behavioral round focused on teamwork.",
        "top_reference_links": ["https://leetcode.com/discuss/12345"],
    })
    text = planner._answer_card_text(drive, _profile(), None, None, None)
    assert "🔗 Study materials: https://leetcode.com/discuss/12345" in text
    assert "|" not in text


def test_answer_card_omits_prep_intel_section_when_never_attempted():
    drive = _drive(prep_intel=None)
    text = planner._answer_card_text(drive, _profile(), None, None, None)
    assert "Prep Strategy" not in text
    assert "Study materials" not in text
    assert "💡" not in text


def test_answer_card_omits_prep_intel_section_when_attempted_but_nothing_found():
    """The search/synthesis pipeline always marks an attempt (never
    re-searches the same drive), but a summary of None means nothing was
    found -- the card must fall back cleanly, no broken variables or empty
    headers, exactly as if it had never been attempted at all."""
    drive = _drive(prep_intel={"attempted": True, "strategy_summary": None, "top_reference_links": []})
    text = planner._answer_card_text(drive, _profile(), None, None, None)
    assert "Prep Strategy" not in text
    assert "💡" not in text


def test_answer_card_without_prep_intel_matches_the_original_layout_exactly():
    """Regression guard: enabling this feature must never change the card
    for anyone who hasn't opted in (ENABLE_PREP_INTEL=0, the default) --
    drive.prep_intel simply doesn't exist for them."""
    drive = _drive()
    resume = ResumeFile(file_id="f1", name="resume_SDE.pdf", web_view_link="https://drive/sde")
    text = planner._answer_card_text(drive, _profile(), resume, "Matches the JD.", "https://forms.gle/prefilled")
    assert text == (
        "Zentrix Analytics — Software Engineer\n"
        "Riya Mehta (21BCS045, CSE)\n"
        "GPA: 7.42\n"
        f"Deadline: {planner._format_deadline(NOW)}\n"
        "Resume: https://drive/sde\n"
        "Why this one: Matches the JD.\n"
        "Form (pre-filled, just review & submit): https://forms.gle/prefilled"
    )


def test_answer_card_prep_intel_survives_alongside_resume_and_form_lines():
    drive = _drive(prep_intel={
        "attempted": True, "strategy_summary": "Expect graph and DP questions.",
        "top_reference_links": ["https://leetcode.com/discuss/12345"],
    })
    resume = ResumeFile(file_id="f1", name="resume_SDE.pdf", web_view_link="https://drive/sde")
    text = planner._answer_card_text(drive, _profile(), resume, "Matches the JD.", None)
    assert "Resume: https://drive/sde" in text
    assert "Why this one: Matches the JD." in text
    assert "Form: not found, check the email" in text
    assert "💡 Prep Strategy: Expect graph and DP questions." in text

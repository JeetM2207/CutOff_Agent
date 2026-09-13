"""llm/prompts.py's SYSTEM_PROMPT (Section 9). Can't unit-test an LLM's
actual classification behavior hermetically, but this pins the specific
rule text in place — regression coverage for a live finding (self_heal.py's
second diagnosis, heldout_007): an email describing an already-closed drive,
shared "for your records," got classified NON_DRIVE with no explicit rule
telling the model otherwise. Verified live (not just by this text check)
that adding the rule fixed the real extraction call — see NOTES.md."""
from cutoff.llm.prompts import SYSTEM_PROMPT


def test_system_prompt_distinguishes_closed_drive_from_true_non_drive():
    lowered = SYSTEM_PROMPT.lower()
    assert "non_drive" in lowered
    assert "already closed" in lowered or "already happened" in lowered
    assert "never use non_drive just because a deadline is in the past" in lowered


def test_system_prompt_distinguishes_generic_aggregate_from_10th_12th_pct():
    """Live finding while writing docs/RELIABILITY_BRIEF.md: a generic "60%
    aggregate throughout academics" cutoff, with no mention of 10th/12th
    anywhere in the email, was extracted into BOTH min_10th_pct and
    min_12th_pct (reproduced 3/3 live). The student's profile happened to
    clear both, so the deterministic engine returned ELIGIBLE and sent a
    Telegram approval for a drive whose real (untracked) requirement should
    have triggered NEEDS_REVIEW. See NOTES.md and the reliability brief."""
    lowered = SYSTEM_PROMPT.lower()
    assert "min_10th_pct" in lowered and "min_12th_pct" in lowered
    assert "other_conditions" in lowered
    assert "aggregate" in lowered


def test_system_prompt_requires_extracting_test_interview_events():
    """Live finding (dev_023_test_slot_clashes_with_exam): the tool schema's
    `events` field had zero natural-language guidance anywhere in the prompt
    — nothing told the model it existed or when to use it. Reproduced 8
    identical extract_notice() calls on an email unambiguously stating an
    online test's date/time: 2 of 8 returned events=[] entirely, silently
    dropping the test slot (and, downstream, the exam-clash check and
    calendar event that depend on it). 10/10 correct after adding this rule.
    See NOTES.md and the reliability brief."""
    lowered = SYSTEM_PROMPT.lower()
    assert "events" in lowered
    assert "test" in lowered and "interview" in lowered
    assert "never leave" in lowered and "events empty" in lowered


def test_system_prompt_requires_company_even_when_it_overlaps_role_wording():
    """Live finding (dev_004_not_eligible_backlogs / dev_019_deadline_already_passed):
    "Solstice Cloud is hiring Cloud Support Engineers" extracted company=None
    8/8 identical calls, while a control email with no word overlap between
    company and role ("Zentrix Analytics" / "Data Analyst") extracted
    correctly every time — the model was folding a company name into the
    role field whenever they shared a word ("Cloud"/"Cloud", "Finance"/
    "Finance"). Downstream, drive_id fell back to "unknown:...", silently
    writing the sheet row under the wrong key. 0/8 failures after adding
    this rule. See NOTES.md and the reliability brief."""
    lowered = SYSTEM_PROMPT.lower()
    assert "solstice cloud" in lowered and "cloud support engineer" in lowered
    assert "never drop company" in lowered

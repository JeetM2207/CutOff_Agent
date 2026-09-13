"""Every rule and edge case in Section 10, as its own test."""
from datetime import datetime, timezone

from cutoff.models import CollegePolicy, Criteria, Drive, Notice, StudentProfile, Verdict
from cutoff.pipeline.eligibility import evaluate

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def profile(**overrides) -> StudentProfile:
    base = dict(
        name="Riya Mehta", roll_no="21BCS045", email="riya@college.edu", branch="CSE",
        gpa=7.42, active_backlogs=0, pct_10th=91, pct_12th=86, batch_year=2026,
        placed_status="UNPLACED", current_offer_lpa=None,
    )
    base.update(overrides)
    return StudentProfile(**base)


def policy(**overrides) -> CollegePolicy:
    base = dict(college_domain="college.edu", one_offer_rule=True, dream_multiplier=1.5)
    base.update(overrides)
    return CollegePolicy(**base)


def drive(criteria: Criteria, **overrides) -> Drive:
    base = dict(drive_id="d1", company="Zentrix", role="Data Analyst", criteria=criteria)
    base.update(overrides)
    return Drive(**base)


def notice(unverified_fields=None) -> Notice:
    return Notice(notice_type="NEW_DRIVE", unverified_fields=unverified_fields or [])


def test_gpa_below_cutoff_fails():
    v = evaluate(drive(Criteria(min_gpa=7.5)), notice(), profile(gpa=6.98), policy(), NOW)
    assert v.result == "NOT_ELIGIBLE"
    assert v.reasons == ["GPA"]


def test_gpa_above_cutoff_passes():
    v = evaluate(drive(Criteria(min_gpa=7.0)), notice(), profile(gpa=7.42), policy(), NOW)
    assert v.result == "ELIGIBLE"


def test_gpa_exactly_at_cutoff_inclusive_passes():
    v = evaluate(drive(Criteria(min_gpa=7.0, gpa_inclusive=True)), notice(), profile(gpa=7.0), policy(), NOW)
    assert v.result == "ELIGIBLE"


def test_gpa_exactly_at_cutoff_exclusive_fails():
    v = evaluate(drive(Criteria(min_gpa=7.0, gpa_inclusive=False)), notice(), profile(gpa=7.0), policy(), NOW)
    assert v.result == "NOT_ELIGIBLE"
    assert v.reasons == ["GPA"]


def test_gpa_exactly_at_cutoff_unclear_needs_review():
    v = evaluate(drive(Criteria(min_gpa=7.0, gpa_inclusive=None)), notice(), profile(gpa=7.0), policy(), NOW)
    assert v.result == "NEEDS_REVIEW"
    assert "7.0" in v.questions[0]


def test_gpa_near_miss_is_not_eligible_not_review():
    v = evaluate(drive(Criteria(min_gpa=7.0)), notice(), profile(gpa=6.98), policy(), NOW)
    assert v.result == "NOT_ELIGIBLE"
    assert v.reasons == ["GPA"]


def test_percentage_cutoff_with_gpa_only_profile_is_needs_review():
    c = Criteria(other_conditions=["60% aggregate throughout academics"])
    v = evaluate(drive(c), notice(), profile(), policy(one_offer_rule=False), NOW)
    assert v.result == "NEEDS_REVIEW"


def test_branch_not_allowed_fails():
    v = evaluate(drive(Criteria(branches_allowed=["ECE"], branches_text="ECE only")), notice(),
                 profile(branch="CSE"), policy(one_offer_rule=False), NOW)
    assert v.result == "NOT_ELIGIBLE"
    assert v.reasons == ["BRANCH"]


def test_branch_allowed_passes():
    v = evaluate(drive(Criteria(branches_allowed=["CSE", "IT"], branches_text="CSE, IT")), notice(),
                 profile(branch="CSE"), policy(one_offer_rule=False), NOW)
    assert v.result == "ELIGIBLE"


def test_vague_branch_wording_is_needs_review():
    c = Criteria(branches_allowed=["ECE"], branches_text="CSE and allied branches")
    v = evaluate(drive(c), notice(), profile(branch="CSE"), policy(one_offer_rule=False), NOW)
    assert v.result == "NEEDS_REVIEW"


def test_vague_branch_wording_but_student_explicitly_listed_passes():
    c = Criteria(branches_allowed=["CSE"], branches_text="CSE and allied branches")
    v = evaluate(drive(c), notice(), profile(branch="CSE"), policy(one_offer_rule=False), NOW)
    assert v.result == "ELIGIBLE"


def test_vague_branch_word_still_needs_review_even_if_llm_guessed_the_student_in():
    # Found live (self_heal.py's first real diagnosis, heldout_003): the same
    # vague phrase ("circuit branches") extracted differently across live
    # LLM calls — branches_allowed sometimes included the student's branch,
    # sometimes not. The student's branch code isn't literally written
    # anywhere in branches_text here (only "circuit" and "CSE" are), so its
    # presence in branches_allowed is purely the LLM's own interpretation of
    # a vague term, not a confirmed explicit mention — must still ask,
    # unlike the "CSE and allied branches" / CSE-student case above where
    # CSE genuinely is named.
    c = Criteria(branches_allowed=["CSE", "ME", "MME"], branches_text="circuit branches and CSE")
    v = evaluate(drive(c), notice(), profile(branch="MME"), policy(one_offer_rule=False), NOW)
    assert v.result == "NEEDS_REVIEW"


def test_no_branch_rule_stated_passes():
    v = evaluate(drive(Criteria()), notice(), profile(branch="ECE"), policy(one_offer_rule=False), NOW)
    assert v.result == "ELIGIBLE"


def test_branches_text_present_but_branches_allowed_empty_needs_review():
    # Found live (dev_010_vague_branches): extract_notice() on "CSE and
    # allied branches" returned branches_allowed=[] in 4 of 5 identical
    # calls, even though branches_text was correctly populated every time.
    # `if criteria.branches_allowed:` alone can't distinguish that from a
    # genuine "no branch rule stated" (Criteria() default is also []), and
    # would silently skip the whole branch check, letting an unresolved
    # branch restriction fall through to ELIGIBLE instead of asking.
    c = Criteria(branches_allowed=[], branches_text="CSE and allied branches")
    v = evaluate(drive(c), notice(), profile(branch="IT"), policy(one_offer_rule=False), NOW)
    assert v.result == "NEEDS_REVIEW"


def test_backlogs_over_limit_fails():
    v = evaluate(drive(Criteria(max_active_backlogs=0)), notice(), profile(active_backlogs=1),
                 policy(one_offer_rule=False), NOW)
    assert v.result == "NOT_ELIGIBLE"
    assert v.reasons == ["BACKLOGS"]


def test_backlogs_within_limit_passes():
    v = evaluate(drive(Criteria(max_active_backlogs=1)), notice(), profile(active_backlogs=1),
                 policy(one_offer_rule=False), NOW)
    assert v.result == "ELIGIBLE"


def test_10th_pct_below_minimum_fails():
    v = evaluate(drive(Criteria(min_10th_pct=90)), notice(), profile(pct_10th=85),
                 policy(one_offer_rule=False), NOW)
    assert v.result == "NOT_ELIGIBLE"
    assert v.reasons == ["PCT_10"]


def test_12th_pct_below_minimum_fails():
    v = evaluate(drive(Criteria(min_12th_pct=90)), notice(), profile(pct_12th=85),
                 policy(one_offer_rule=False), NOW)
    assert v.result == "NOT_ELIGIBLE"
    assert v.reasons == ["PCT_12"]


def test_10th_pct_missing_from_profile_is_needs_review():
    v = evaluate(drive(Criteria(min_10th_pct=90)), notice(), profile(pct_10th=None),
                 policy(one_offer_rule=False), NOW)
    assert v.result == "NEEDS_REVIEW"


def test_batch_year_not_allowed_fails():
    v = evaluate(drive(Criteria(batch_years=[2025])), notice(), profile(batch_year=2026),
                 policy(one_offer_rule=False), NOW)
    assert v.result == "NOT_ELIGIBLE"
    assert v.reasons == ["BATCH"]


def test_batch_year_allowed_passes():
    v = evaluate(drive(Criteria(batch_years=[2026])), notice(), profile(batch_year=2026),
                 policy(one_offer_rule=False), NOW)
    assert v.result == "ELIGIBLE"


def test_unverified_field_is_needs_review():
    v = evaluate(drive(Criteria(min_gpa=7.0)), notice(unverified_fields=["criteria.min_gpa"]),
                 profile(gpa=8.0), policy(one_offer_rule=False), NOW)
    assert v.result == "NEEDS_REVIEW"
    assert "GPA" in v.questions[0]


def test_policy_one_offer_placed_below_dream_threshold_fails():
    v = evaluate(
        drive(Criteria(), salary_lpa=9.0),
        notice(), profile(placed_status="PLACED", current_offer_lpa=7.0),
        policy(one_offer_rule=True, dream_multiplier=1.5), NOW,
    )
    assert v.result == "NOT_ELIGIBLE"
    assert v.reasons == ["POLICY_ONE_OFFER"]


def test_policy_one_offer_placed_dream_drive_passes():
    v = evaluate(
        drive(Criteria(), salary_lpa=12.0),
        notice(), profile(placed_status="PLACED", current_offer_lpa=7.0),
        policy(one_offer_rule=True, dream_multiplier=1.5), NOW,
    )
    assert v.result == "ELIGIBLE"


def test_policy_one_offer_placed_salary_unknown_is_needs_review():
    v = evaluate(
        drive(Criteria(), salary_lpa=None),
        notice(), profile(placed_status="PLACED", current_offer_lpa=7.0),
        policy(one_offer_rule=True, dream_multiplier=1.5), NOW,
    )
    assert v.result == "NEEDS_REVIEW"


def test_policy_one_offer_does_not_apply_when_unplaced():
    v = evaluate(
        drive(Criteria(), salary_lpa=5.0),
        notice(), profile(placed_status="UNPLACED"),
        policy(one_offer_rule=True, dream_multiplier=1.5), NOW,
    )
    assert v.result == "ELIGIBLE"


def test_fail_always_wins_over_unclear():
    c = Criteria(min_gpa=9.0, branches_allowed=["ECE"], branches_text="allied branches")
    v = evaluate(drive(c), notice(), profile(branch="CSE", gpa=7.0), policy(one_offer_rule=False), NOW)
    assert v.result == "NOT_ELIGIBLE"
    assert v.reasons == ["GPA"]


def test_multiple_fail_reasons_all_listed():
    c = Criteria(min_gpa=9.0, max_active_backlogs=0)
    v = evaluate(drive(c), notice(), profile(gpa=6.0, active_backlogs=1), policy(one_offer_rule=False), NOW)
    assert v.result == "NOT_ELIGIBLE"
    assert set(v.reasons) == {"GPA", "BACKLOGS"}

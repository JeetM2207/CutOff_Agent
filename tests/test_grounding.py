"""Section 8 step 4: every non-null field needs a verbatim evidence quote, or
it's nulled and added to unverified_fields."""
from cutoff.llm.grounding import validate_grounding
from cutoff.models import Criteria, Evidence, Notice

SOURCE = (
    "subject: Campus Drive: Zentrix Analytics\n"
    "Dear Students,\nZentrix Analytics is visiting for the Data Analyst role.\n"
    "Eligibility: CGPA 7.0 and above. No active backlogs.\n"
    "Register by 11:59 PM, 12 September 2026."
)


def test_grounded_field_survives():
    notice = Notice(
        notice_type="NEW_DRIVE", company="Zentrix Analytics",
        criteria=Criteria(min_gpa=7.0),
        evidence=[Evidence(field="company", quote="Zentrix Analytics"),
                  Evidence(field="criteria.min_gpa", quote="CGPA 7.0 and above")],
    )
    out = validate_grounding(notice, SOURCE)
    assert out.company == "Zentrix Analytics"
    assert out.criteria.min_gpa == 7.0
    assert out.unverified_fields == []


def test_hallucinated_field_is_nulled_and_flagged():
    notice = Notice(
        notice_type="NEW_DRIVE", company="Zentrix Analytics",
        criteria=Criteria(min_gpa=9.0),  # not actually in the source
        evidence=[Evidence(field="company", quote="Zentrix Analytics"),
                  Evidence(field="criteria.min_gpa", quote="CGPA 9.0 and above")],
    )
    out = validate_grounding(notice, SOURCE)
    assert out.criteria.min_gpa is None
    assert "criteria.min_gpa" in out.unverified_fields
    assert out.company == "Zentrix Analytics"  # untouched: its own quote was grounded


def test_quote_matching_is_whitespace_and_case_insensitive():
    notice = Notice(
        notice_type="NEW_DRIVE",
        evidence=[Evidence(field="company", quote="  ZENTRIX   analytics  ")],
        company="Zentrix Analytics",
    )
    out = validate_grounding(notice, SOURCE)
    assert out.company == "Zentrix Analytics"
    assert out.unverified_fields == []


def test_list_field_is_cleared_not_nulled():
    notice = Notice(
        notice_type="NEW_DRIVE",
        criteria=Criteria(branches_allowed=["CSE", "IT"]),
        evidence=[Evidence(field="criteria.branches_allowed", quote="totally made up branch text")],
    )
    out = validate_grounding(notice, SOURCE)
    assert out.criteria.branches_allowed == []
    assert "criteria.branches_allowed" in out.unverified_fields


def test_empty_quote_is_never_grounded():
    notice = Notice(
        notice_type="NEW_DRIVE", company="Zentrix Analytics",
        evidence=[Evidence(field="company", quote="")],
    )
    out = validate_grounding(notice, SOURCE)
    assert out.company is None
    assert "company" in out.unverified_fields


# --- Phase 4 fix: near-verbatim quotes (found live — a non-hallucinating
# model routinely drops one connective word) must survive, but a wrong
# number must still be rejected even though it's a tiny edit-distance away.

def test_quote_missing_one_connective_word_still_grounds():
    notice = Notice(
        notice_type="NEW_DRIVE", role="Data Analyst",
        # source says "visiting for the Data Analyst role" — "role" dropped
        evidence=[Evidence(field="role", quote="visiting for the Data Analyst")],
    )
    out = validate_grounding(notice, SOURCE)
    assert out.role == "Data Analyst"
    assert out.unverified_fields == []


def test_hallucinated_number_is_rejected_even_though_its_a_small_edit_distance():
    # "9.0" vs the real "7.0" differs by one character — naive fuzzy string
    # similarity would score this as a close match. It must still be nulled.
    notice = Notice(
        notice_type="NEW_DRIVE", criteria=Criteria(min_gpa=9.0),
        evidence=[Evidence(field="criteria.min_gpa", quote="CGPA 9.0 and above")],
    )
    out = validate_grounding(notice, SOURCE)
    assert out.criteria.min_gpa is None
    assert "criteria.min_gpa" in out.unverified_fields


def test_fabricated_claim_built_from_common_words_is_rejected():
    notice = Notice(
        notice_type="NEW_DRIVE",
        criteria=Criteria(other_conditions=["placement guaranteed for all students"]),
        evidence=[Evidence(field="criteria.other_conditions", quote="placement guaranteed for all students")],
    )
    out = validate_grounding(notice, SOURCE)
    assert out.criteria.other_conditions == []
    assert "criteria.other_conditions" in out.unverified_fields

"""Deterministic eligibility and policy engine (Section 10). Pure functions,
no I/O. The LLM never decides eligibility — this does."""
from __future__ import annotations

import re
from datetime import datetime

from cutoff.models import CollegePolicy, Criteria, Drive, Notice, StudentProfile, Verdict

VAGUE_BRANCH_WORDS = ("allied", "circuit", "related", "except core", "and similar")

# Maps a Notice.unverified_fields entry to the human label used in a NEEDS_REVIEW question.
_UNVERIFIED_FIELD_LABELS = {
    "criteria.min_gpa": "GPA",
    "criteria.gpa_inclusive": "GPA",
    "criteria.branches_allowed": "branch",
    "criteria.branches_text": "branch",
    "criteria.max_active_backlogs": "backlogs",
    "criteria.min_10th_pct": "10th percentage",
    "criteria.min_12th_pct": "12th percentage",
    "criteria.batch_years": "batch year",
}

_PCT_PATTERN = re.compile(r"\b\d{1,3}\s*%")


def _dedupe(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _mentions_percentage_cutoff(criteria: Criteria) -> bool:
    return any(_PCT_PATTERN.search(cond) for cond in criteria.other_conditions)


def evaluate(drive: Drive, notice: Notice, profile: StudentProfile, policy: CollegePolicy, now: datetime) -> Verdict:
    criteria = drive.criteria
    fails: list[str] = []
    questions: list[str] = []

    # GPA
    if criteria.min_gpa is not None:
        if profile.gpa < criteria.min_gpa:
            fails.append("GPA")
        elif profile.gpa == criteria.min_gpa:
            if criteria.gpa_inclusive is False:
                fails.append("GPA")
            elif criteria.gpa_inclusive is None:
                questions.append(
                    f"The cutoff is {criteria.min_gpa} and your GPA is exactly {profile.gpa}. "
                    "The email doesn't say if that counts. Check with the career office?"
                )

    # GPA scale: a percentage cutoff stated with no matching field on a GPA-only profile.
    if _mentions_percentage_cutoff(criteria) and criteria.min_10th_pct is None and criteria.min_12th_pct is None:
        questions.append(
            "The email asks for a percentage cutoff. Your college's GPA-to-% conversion isn't in your profile."
        )

    # Branch
    if criteria.branches_allowed or criteria.branches_text:
        vague = any(w in (criteria.branches_text or "").lower() for w in VAGUE_BRANCH_WORDS)
        # "CSE and allied branches" for a CSE student isn't ambiguous — CSE
        # is named explicitly; "allied" only covers *other*, unspecified
        # branches. Only treat this student's own match as unreliable when
        # their branch code isn't literally written in the source text —
        # i.e. branches_allowed included them purely from the LLM's own
        # interpretation of the vague term, not from an explicit mention.
        named_explicitly = profile.branch.lower() in (criteria.branches_text or "").lower()
        if not criteria.branches_allowed:
            # Found live: for the exact same vague phrase ("CSE and allied
            # branches"), extract_notice() sometimes returns branches_allowed
            # as an empty list instead of at least ["CSE"] — non-determinism,
            # not a real "no branch rule" case, since branches_text is still
            # populated. `if criteria.branches_allowed:` alone can't tell
            # those two apart, and a truly empty list would silently skip
            # this whole block, letting an unresolved branch restriction
            # fall through to ELIGIBLE. Ask instead of guessing.
            questions.append(
                f"The email says \"{criteria.branches_text}\" but I couldn't tell exactly which "
                f"branches that covers. Check with the career office?"
            )
        elif profile.branch not in criteria.branches_allowed:
            if vague:
                questions.append(
                    f"The email says \"{criteria.branches_text}\". It's not clear if {profile.branch} "
                    "counts. Check with the career office?"
                )
            else:
                fails.append("BRANCH")
        elif vague and not named_explicitly:
            # Found live: the LLM's own branches_allowed guess for a vague
            # phrase is unreliable in either direction — the same wording
            # extracted differently across runs, sometimes (wrongly)
            # including the student's branch. Trusting a guess that happens
            # to include them would silently skip the review question
            # exactly when it matters most.
            questions.append(
                f"The email says \"{criteria.branches_text}\". It's not clear if {profile.branch} "
                "counts. Check with the career office?"
            )

    # Backlogs
    if criteria.max_active_backlogs is not None and profile.active_backlogs > criteria.max_active_backlogs:
        fails.append("BACKLOGS")

    # 10th / 12th %
    if criteria.min_10th_pct is not None:
        if profile.pct_10th is None:
            questions.append("The email requires a minimum 10th percentage, but that's missing from your profile.")
        elif profile.pct_10th < criteria.min_10th_pct:
            fails.append("PCT_10")
    if criteria.min_12th_pct is not None:
        if profile.pct_12th is None:
            questions.append("The email requires a minimum 12th percentage, but that's missing from your profile.")
        elif profile.pct_12th < criteria.min_12th_pct:
            fails.append("PCT_12")

    # Batch year
    if criteria.batch_years and profile.batch_year not in criteria.batch_years:
        fails.append("BATCH")

    # Unverified fields (grounding failures)
    for field_path in notice.unverified_fields:
        label = _UNVERIFIED_FIELD_LABELS.get(field_path)
        if label:
            questions.append(f"I couldn't confirm the {label} rule in the email text.")

    # Policy: one offer
    if profile.placed_status == "PLACED" and policy.one_offer_rule:
        salary = drive.salary_lpa
        if salary is None:
            questions.append(
                "You're placed, and this drive's salary isn't stated. Check the dream-offer rule with the career office?"
            )
        elif profile.current_offer_lpa is not None and salary < profile.current_offer_lpa * policy.dream_multiplier:
            fails.append("POLICY_ONE_OFFER")

    if fails:
        return Verdict(result="NOT_ELIGIBLE", reasons=_dedupe(fails), questions=[])
    if questions:
        return Verdict(result="NEEDS_REVIEW", reasons=[], questions=_dedupe(questions))
    return Verdict(result="ELIGIBLE")

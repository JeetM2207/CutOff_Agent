"""Builds a pre-filled registration-form link from a stored template
(Section 6.5 extension). The Forms API can only read a form's own question
structure if the caller owns it or has edit access — a recruiter's form
never qualifies, so there is no way to auto-discover a brand-new company
form's fields. Instead, the *template* is captured once per distinct form
layout using Google's own "Get pre-filled link" feature, which any
respondent can use (no ownership needed): open the form, type the literal
placeholder tokens below into each field, use "Get pre-filled link", and
paste the resulting URL into the Sheet's FormTemplates tab next to that
drive's form_url. At runtime, this module swaps each placeholder token for
the student's real profile value.

This never submits the form — Section 6.5's guarantee holds: the student
still clicks Submit themselves. Falls back to the plain form_url (by
returning None) whenever no template is on file, or substitution finds
nothing to replace."""
from __future__ import annotations

import logging
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from cutoff.models import StudentProfile

logger = logging.getLogger("cutoff.form_prefill")

# Type these exact strings (case-insensitive) into a form's fields before
# using "Get pre-filled link" — whichever entry ID ends up holding one of
# these tokens gets that profile field substituted in at runtime.
PLACEHOLDER_TOKENS: dict[str, str] = {
    "student_name": "name",
    "student_roll_no": "roll_no",
    "student_branch": "branch",
    "student_email": "email",
    "student_gpa": "gpa",
}


def build_prefilled_url(template_url: str | None, profile: StudentProfile) -> str | None:
    if not template_url:
        return None
    try:
        parsed = urlparse(template_url)
        params = parse_qsl(parsed.query, keep_blank_values=True)
        substituted = []
        matched_any = False
        for key, value in params:
            field = PLACEHOLDER_TOKENS.get(value.strip().lower())
            real_value = getattr(profile, field, None) if field else None
            if real_value is not None:
                value = str(real_value)
                matched_any = True
            substituted.append((key, value))
        if not matched_any:
            return None
        return urlunparse(parsed._replace(query=urlencode(substituted)))
    except Exception:
        logger.exception("form prefill substitution failed; falling back to plain form_url")
        return None

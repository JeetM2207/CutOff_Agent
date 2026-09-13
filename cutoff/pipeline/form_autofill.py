"""Automatically detects a Google Form's own fields (Section 6.5 extension)
and fills what it safely can, so the Telegram approval card carries a link
that's already filled in to review and submit — never auto-submitted.

Two layers:
1. Known profile fields (name/roll number/branch/email/GPA), matched by
   keyword against the question's own title — deterministic, no LLM.
2. Open-ended short-answer/paragraph questions the LLM can ground in the
   student's chosen resume text + this drive's job-description text. Never
   a multiple-choice/checkbox/dropdown/date/time question — those need the
   student's own judgment (`forms_scrape.FILLABLE_TYPES` already excludes
   them at the parsing layer).

Returns None on any failure (unreadable form, network error, nothing
fillable) — the caller then tries the stored-template prefill
(form_prefill.py), then the plain form link. Nothing here ever submits the
form."""
from __future__ import annotations

import logging
from urllib.parse import urlencode

from cutoff.adapters.forms_scrape import FILLABLE_TYPES, fetch_form_page, parse_form_fields
from cutoff.models import StudentProfile

logger = logging.getLogger("cutoff.form_autofill")

_FIELD_KEYWORDS: list[tuple[str, str]] = [
    ("roll", "roll_no"), ("registration no", "roll_no"), ("enrollment", "roll_no"),
    ("full name", "name"), ("name", "name"),
    ("branch", "branch"), ("department", "branch"), ("stream", "branch"),
    ("e-mail", "email"), ("email", "email"),
    ("cgpa", "gpa"), ("gpa", "gpa"), ("grade", "gpa"),
]

# Checked before profile-field keywords and before the LLM fallback: a
# "resume link" field needs the actual Drive URL, not generated prose — the
# LLM has no way to produce a real link from resume text alone, so without
# this it would either fabricate one or (per its own grounding instructions)
# correctly leave the field blank. Either way, deterministic is better than
# asking an LLM for something it can't ground.
_RESUME_LINK_KEYWORDS = ("resume", "cv", "cover letter")


def _match_profile_field(title: str) -> str | None:
    lowered = title.lower()
    for keyword, field in _FIELD_KEYWORDS:
        if keyword in lowered:
            return field
    return None


def build_autofilled_url(
    form_url: str, profile: StudentProfile, resume_text: str, jd_text: str,
    *, resume_link: str | None = None, api_key: str, model: str, provider: str, base_url: str | None,
) -> str | None:
    if not form_url:
        return None
    try:
        page = fetch_form_page(form_url)
        if page is None:
            return None
        viewform_url, html = page
        fields = [f for f in parse_form_fields(html) if f.field_type in FILLABLE_TYPES]
        if not fields:
            return None

        entries: dict[str, str] = {}
        open_questions: list[tuple[str, str]] = []  # (title, entry_id)
        for f in fields:
            lowered_title = f.title.lower()
            if resume_link and any(kw in lowered_title for kw in _RESUME_LINK_KEYWORDS):
                entries[f"entry.{f.entry_id}"] = resume_link
                continue
            profile_field = _match_profile_field(f.title)
            value = getattr(profile, profile_field, None) if profile_field else None
            if value is not None:
                entries[f"entry.{f.entry_id}"] = str(value)
            elif profile_field is None:
                open_questions.append((f.title, f.entry_id))

        if open_questions and resume_text.strip():
            from cutoff.llm.form_answer import draft_answers

            titles = [t for t, _ in open_questions]
            try:
                answers = draft_answers(
                    resume_text, jd_text, titles,
                    api_key=api_key, model=model, provider=provider, base_url=base_url,
                )
            except Exception:
                logger.exception("form answer drafting failed; leaving open-ended questions blank")
                answers = {}
            for title, entry_id in open_questions:
                answer = answers.get(title, "").strip()
                if answer:
                    entries[f"entry.{entry_id}"] = answer

        if not entries:
            return None
        base = viewform_url.split("?")[0]
        return f"{base}?usp=pp_url&{urlencode(entries)}"
    except Exception:
        logger.exception("form auto-fill failed; falling back to stored template or plain link")
        return None

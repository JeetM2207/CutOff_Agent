"""Reads a public Google Form's own question list straight from the page it
already serves every respondent — no OAuth, no ownership needed. The
official Forms API can only read a form the caller owns or has edit access
to, which a recruiter's form never is, so there is no authenticated way to
auto-detect a third party's form fields (confirmed before building this —
see NOTES.md). Google embeds the form's full structure client-side in a
`FB_PUBLIC_LOAD_DATA_` JS variable on the public viewform page; this parses
that. Unofficial and undocumented, but a long-stable, widely used technique
(e.g. https://github.com/corazonthedev/google-form-parser). Every failure
here is non-fatal to the caller: a page-format change just means "couldn't
auto-read this form," never a crash — form_autofill.py falls back to a
stored template, then the plain form link."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

_FORM_ID_RE = re.compile(r"/forms/d/(?:e/)?([a-zA-Z0-9_-]+)")
_LOAD_DATA_MARKER = "FB_PUBLIC_LOAD_DATA_"

# Question type codes (from the reference structure): 0=short answer,
# 1=paragraph, 2=multiple choice, 3=dropdown, 4=checkboxes, 5=linear scale,
# 7=grid, 9=date, 10=time. Only free-text types are ever auto-filled — a
# fabricated choice/checkbox/date pick could be actively wrong (years of
# experience, work-authorization consent, interview slot), so those are
# always left for the student to pick themselves after opening the link.
FILLABLE_TYPES = {0, 1}


@dataclass
class FormField:
    entry_id: str
    title: str
    field_type: int
    required: bool


def fetch_form_page(form_url: str) -> tuple[str, str] | None:
    """Follows any redirect (forms.gle is a shortener) and returns
    (resolved_viewform_url, html). None on any network failure, or if the
    resolved URL doesn't look like a Google Form at all."""
    try:
        resp = httpx.get(form_url, follow_redirects=True, timeout=10)
        resp.raise_for_status()
    except httpx.HTTPError:
        return None
    final_url = str(resp.url)
    if not _FORM_ID_RE.search(final_url):
        return None
    return final_url, resp.text


def _extract_balanced_array(html: str, marker: str) -> str | None:
    """A regex can't safely bracket-match JSON containing arbitrary
    question text (which may itself contain "[" / "]" / escaped quotes), so
    this scans character by character, tracking string state, to find the
    exact span of the top-level array following `marker`."""
    idx = html.find(marker)
    if idx == -1:
        return None
    start = html.find("[", idx)
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(html)):
        ch = html[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return html[start : i + 1]
    return None


def parse_form_fields(html: str) -> list[FormField]:
    """Every question on the form, fillable or not — caller filters by
    FILLABLE_TYPES. Empty list (never an exception) if the page's structure
    doesn't match what's expected, e.g. the page isn't actually a Google
    Form, or Google changes the embedded format."""
    raw = _extract_balanced_array(html, _LOAD_DATA_MARKER)
    if raw is None:
        return []
    try:
        data = json.loads(raw)
        questions = data[1][1] or []
    except (json.JSONDecodeError, IndexError, TypeError):
        return []

    fields: list[FormField] = []
    for q in questions:
        try:
            title = q[1]
            field_type = q[3]
            entries = q[4]
            if not title or not entries:
                continue
            entry_id = entries[0][0]
            required = bool(entries[0][2]) if len(entries[0]) > 2 else False
        except (IndexError, TypeError):
            continue
        fields.append(FormField(entry_id=str(entry_id), title=title, field_type=field_type, required=required))
    return fields

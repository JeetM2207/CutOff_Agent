"""Grounding validator (Section 8, step 4): every non-null field must have an
evidence quote that appears verbatim (after whitespace/case normalization) in
the email text. A field that fails is nulled and added to `unverified_fields`.

"Verbatim" is relaxed slightly from exact-substring to near-verbatim (see
`_is_near_verbatim`) — found live (Phase 2 baseline) that a non-hallucinating
model's evidence quotes routinely drop one connective word ("the Software
Engineer role" -> quoted as "Software Engineer"), and exact-substring
matching nulled the field anyway. Plain fuzzy string similarity (tried first)
is unsafe for this: a hallucinated "CGPA 9.0" vs the real "CGPA 7.0" scores
*higher* similarity than the legitimate paraphrase above, since a wrong
digit is a tiny edit-distance change. So numbers are matched exactly (any
number in the quote must appear verbatim somewhere in the source) while
words are matched as a bag (most of them must appear somewhere in the
source) — this tolerates dropped connectives without tolerating a wrong
number or a fabricated claim built from common words."""
from __future__ import annotations

import re

from cutoff.models import Notice

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_WORD_RE = re.compile(r"[a-z0-9]+")
_MIN_WORD_MATCH_RATIO = 0.85


def _normalize(text: str) -> str:
    return " ".join(text.split()).lower()


def _is_near_verbatim(quote_norm: str, source_norm: str) -> bool:
    if not quote_norm:
        return False
    if quote_norm in source_norm:
        return True

    quote_numbers = set(_NUMBER_RE.findall(quote_norm))
    source_numbers = set(_NUMBER_RE.findall(source_norm))
    if not quote_numbers.issubset(source_numbers):
        return False  # any numeric claim must appear verbatim somewhere in the source

    quote_words = _WORD_RE.findall(quote_norm)
    source_words = set(_WORD_RE.findall(source_norm))
    if not quote_words:
        return False
    present = sum(1 for w in quote_words if w in source_words)
    return (present / len(quote_words)) >= _MIN_WORD_MATCH_RATIO


def validate_grounding(notice: Notice, source_text: str) -> Notice:
    normalized_source = _normalize(source_text)
    data = notice.model_dump(mode="json")
    unverified = list(data.get("unverified_fields", []))

    for ev in notice.evidence:
        quote_norm = _normalize(ev.quote)
        if not _is_near_verbatim(quote_norm, normalized_source):
            _null_field(data, ev.field)
            if ev.field not in unverified:
                unverified.append(ev.field)

    data["unverified_fields"] = unverified
    return Notice.model_validate(data)


def _null_field(data: dict, field_path: str) -> None:
    parts = field_path.split(".")
    obj = data
    for part in parts[:-1]:
        if not isinstance(obj, dict) or part not in obj:
            return
        obj = obj[part]
    if not isinstance(obj, dict):
        return
    last = parts[-1]
    if last not in obj:
        return
    if isinstance(obj[last], list):
        obj[last] = []
    else:
        obj[last] = None

"""Extracts a MasterProfile-shaped fragment from an existing resume's raw
text (Section 6.4 onboarding extension) — forced tool-use, same shape and
grounding discipline as resume_generate.py: only what is literally written
in the resume text may be extracted, nothing inferred or invented. This is
how a student bootstraps config/master_profile.yaml from a resume they
already have, instead of typing everything in by hand. A read, never
ledgered, never blocking anything — the CLI/bot caller decides what to do
with the result."""
from __future__ import annotations

import json

from cutoff.llm.client import call_with_rate_limit_backoff, get_cached, get_client, prompt_hash, set_cached
from cutoff.models import MasterProfile

TOOL_NAME = "extract_master_profile"

_PROFILE_FRAGMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "phone": {"type": ["string", "null"], "description": "Phone number, only if literally present."},
        "links": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"label": {"type": "string"}, "url": {"type": "string"}},
                "required": ["label", "url"],
            },
            "description": "GitHub/LinkedIn/portfolio/etc. links exactly as they appear in the text.",
        },
        "education": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "degree": {"type": "string"}, "institution": {"type": "string"},
                    "cgpa": {"type": ["string", "null"]}, "batch_year": {"type": ["integer", "null"]},
                    "notes": {"type": ["string", "null"]},
                },
                "required": ["degree", "institution"],
            },
        },
        "skills": {"type": "array", "items": {"type": "string"}},
        "projects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "tech_stack": {"type": "array", "items": {"type": "string"}},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                    "link": {"type": ["string", "null"], "description": "The project's repo (e.g. GitHub) URL."},
                    "demo_link": {"type": ["string", "null"], "description": "A live/hosted demo URL, if separate from `link`."},
                },
                "required": ["title", "bullets"],
            },
        },
        "experience": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "bullets": {"type": "array", "items": {"type": "string"}}},
                "required": ["title", "bullets"],
            },
        },
        "achievements": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["links", "education", "skills", "projects", "experience", "achievements"],
}

_SYSTEM = (
    "You are extracting a structured profile from a student's existing resume, verbatim. Every skill, "
    "project, bullet, date, link, and achievement in your output must come directly from the resume "
    "text — never infer, guess, or add anything that isn't explicitly written there. If a field isn't "
    "stated in the resume (e.g. no CGPA given), leave it null rather than estimating one. Rewrite "
    "nothing for style; extract facts as they're written. "
    "`links` means profile URLs only — GitHub, LinkedIn, portfolio site, LeetCode, etc. Never include "
    "the student's email address or phone number as a link; those belong in their own fields. "
    "A resume PDF's hyperlinks (e.g. a name styled as a clickable link reading just \"GitHub\" or "
    "\"Live Demo\") don't carry their real URL in the plain text you're given — the real destination "
    "URLs found in the PDF are listed separately below as REAL LINKS FOUND IN THE DOCUMENT. Match each "
    "one to the right field (a personal GitHub/LinkedIn link, or a specific project's `link`/`demo_link`) "
    "using the surrounding text as context, and use that real URL — never invent one, and never fall back "
    "to using the visible link text (like the word \"GitHub\") as if it were the URL itself. If you can't "
    "confidently match a real link to what it's for, leave that field null rather than guessing wrong. "
    f"Call {TOOL_NAME} with the result."
)


def _call_anthropic(client, model: str, text: str) -> dict:
    tool = {"name": TOOL_NAME, "description": "Record the structured profile extracted from a resume.",
            "input_schema": _PROFILE_FRAGMENT_SCHEMA}
    resp = client.messages.create(
        model=model, max_tokens=4096, temperature=0, system=_SYSTEM,
        tools=[tool], tool_choice={"type": "tool", "name": TOOL_NAME},
        messages=[{"role": "user", "content": text}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == TOOL_NAME:
            return block.input
    raise ValueError(f"model response had no {TOOL_NAME} tool_use block")


def _call_openai_compatible(client, model: str, text: str) -> dict:
    tool = {"type": "function", "function": {
        "name": TOOL_NAME, "description": "Record the structured profile extracted from a resume.",
        "parameters": _PROFILE_FRAGMENT_SCHEMA,
    }}
    resp = client.chat.completions.create(
        model=model, temperature=0, max_tokens=4096,
        tools=[tool], tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
        messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": text}],
    )
    message = resp.choices[0].message
    for call in (getattr(message, "tool_calls", None) or []):
        if call.function.name == TOOL_NAME:
            return json.loads(call.function.arguments)
    raise ValueError(f"model response had no {TOOL_NAME} tool call")


def extract_profile_from_resume_text(
    resume_text: str,
    *, api_key: str, model: str, provider: str, base_url: str | None, db_path: str, use_cache: bool = True,
    hyperlinks: list[str] | None = None,
) -> MasterProfile:
    """Returns a validated MasterProfile. Raises on any failure (empty
    input, LLM error, malformed response) — the CLI/bot caller decides how
    to handle that (never silently produces a half-built profile).

    `hyperlinks` are the REAL URIs embedded in the source PDF (see
    cutoff.pipeline.ingest.extract_pdf_hyperlinks) — plain-text extraction
    alone only shows a link's clickable label ("GitHub"), never its actual
    target, so without this the model has no way to recover a real URL at
    all. Optional (still works, just can't populate links/project URLs
    reliably) so tests and any other resume_text-only caller aren't broken."""
    if not resume_text.strip():
        raise ValueError("no resume text to extract from")

    user_text = f"RESUME TEXT:\n{resume_text.strip()[:15000]}"
    if hyperlinks:
        user_text += "\n\nREAL LINKS FOUND IN THE DOCUMENT:\n" + "\n".join(hyperlinks[:30])
    phash = prompt_hash(provider, model, _SYSTEM, user_text)

    if use_cache:
        cached = get_cached(db_path, phash)
        if cached is not None:
            return MasterProfile(**cached)

    client = get_client(provider, api_key, base_url)
    call_fn = _call_anthropic if provider == "anthropic" else _call_openai_compatible
    result = call_with_rate_limit_backoff(lambda: call_fn(client, model, user_text))
    profile = MasterProfile(**result)  # raises if the model's shape doesn't validate

    if use_cache:
        set_cached(db_path, phash, model, result)
    return profile

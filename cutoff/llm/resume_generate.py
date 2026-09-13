"""Generates a JD-tailored resume from a student's master profile, via forced
tool use — same shape as resume_match.py's pick_best_resume call, just a
different tool and a stricter grounding rule: every skill/project/bullet in
the output must trace back to something actually written in the master
profile, never invented. This is a read, never ledgered, and purely
optional: resume.py catches any failure here and falls back to static
resume_*.pdf matching, so a bad or failed generation never blocks planning."""
from __future__ import annotations

import json

from cutoff.llm.client import call_with_rate_limit_backoff, get_cached, get_client, prompt_hash, set_cached

TOOL_NAME = "generate_tailored_resume"


def _tool_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "headline": {
                "type": "string",
                "description": (
                    "A one-line professional headline summarizing the student for this specific job "
                    "description, grounded only in what's in the master profile below."
                ),
            },
            "skills": {
                "type": "array", "items": {"type": "string"},
                "description": (
                    "Skills from the master profile most relevant to this job description, in priority "
                    "order. Only skills that literally appear in the master profile — never invented."
                ),
            },
            "highlighted_projects": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "bullets": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["title", "bullets"],
                },
                "description": (
                    "The 2-4 projects or experience entries from the master profile most relevant to "
                    "this job description. Rewriting/reordering/rephrasing existing bullets to mirror "
                    "the job description's own terminology is encouraged; adding a project, metric, or "
                    "bullet that isn't in the master profile is not."
                ),
            },
            "match_reason": {
                "type": "string",
                "description": (
                    "One sentence, grounded in the master profile: which specific skills or projects "
                    "were emphasized here and why they fit this job description."
                ),
            },
        },
        "required": ["headline", "skills", "highlighted_projects", "match_reason"],
    }


_SYSTEM = (
    "You are tailoring a student's resume to a specific job description, using ONLY their master "
    "profile below as source material. Select and rephrase the skills, projects, and bullets that "
    "best match this job description's own terminology. Never invent a skill, project, metric, or "
    "bullet point that is not literally present in the master profile — if the profile doesn't "
    "support a strong match, do the best honest job with what's actually there. Call "
    f"{TOOL_NAME} with your result."
)


def _user_text(jd_text: str, master_profile_md: str) -> str:
    return (
        f"JOB DESCRIPTION:\n{jd_text.strip()[:6000]}\n\n"
        f"MASTER PROFILE (the only source of truth — do not add anything not in here):\n"
        f"{master_profile_md.strip()[:12000]}"
    )


def _call_anthropic(client, model: str, system: str, text: str) -> dict:
    tool = {
        "name": TOOL_NAME,
        "description": "Record a job-description-tailored resume built only from the master profile.",
        "input_schema": _tool_schema(),
    }
    resp = client.messages.create(
        model=model, max_tokens=1536, temperature=0, system=system,
        tools=[tool], tool_choice={"type": "tool", "name": TOOL_NAME},
        messages=[{"role": "user", "content": text}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == TOOL_NAME:
            return block.input
    raise ValueError(f"model response had no {TOOL_NAME} tool_use block")


def _call_openai_compatible(client, model: str, system: str, text: str) -> dict:
    tool = {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": "Record a job-description-tailored resume built only from the master profile.",
            "parameters": _tool_schema(),
        },
    }
    resp = client.chat.completions.create(
        model=model, temperature=0, max_tokens=1536,
        tools=[tool], tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": text}],
    )
    message = resp.choices[0].message
    for call in (getattr(message, "tool_calls", None) or []):
        if call.function.name == TOOL_NAME:
            return json.loads(call.function.arguments)
    raise ValueError(f"model response had no {TOOL_NAME} tool call")


def _validate(result: dict) -> dict:
    if not result.get("highlighted_projects"):
        raise ValueError("model returned no highlighted_projects")
    for project in result["highlighted_projects"]:
        if not project.get("title") or not project.get("bullets"):
            raise ValueError(f"malformed project entry: {project!r}")
    return result


def generate_tailored_resume(
    jd_text: str, master_profile_md: str,
    *, api_key: str, model: str, provider: str, base_url: str | None, db_path: str, use_cache: bool = True,
) -> tuple[dict, str]:
    """Returns (sections, match_reason). Raises on any failure — caller
    (resume.select_resume_smart) catches and falls back to static matching."""
    user_text = _user_text(jd_text, master_profile_md)
    phash = prompt_hash(provider, model, _SYSTEM, user_text)

    if use_cache:
        cached = get_cached(db_path, phash)
        if cached is not None:
            return cached, cached["match_reason"]

    client = get_client(provider, api_key, base_url)
    call_fn = _call_anthropic if provider == "anthropic" else _call_openai_compatible
    result = _validate(call_with_rate_limit_backoff(lambda: call_fn(client, model, _SYSTEM, user_text)))

    if use_cache:
        set_cached(db_path, phash, model, result)
    return result, result["match_reason"]

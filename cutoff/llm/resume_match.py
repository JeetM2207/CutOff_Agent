"""Matches a job-description PDF (attached to a drive email) against the
student's resumes on file by actual content, via forced tool use — same
shape as extract.py's record_notice call, just a different tool and a much
smaller prompt. This is a read, never ledgered, and purely informational:
`resume.py` catches any failure here and falls back to the deterministic
role-category filename mapping, so a bad match (or no match) never blocks
planning or registration."""
from __future__ import annotations

import json

from cutoff.llm.client import call_with_rate_limit_backoff, get_cached, get_client, prompt_hash, set_cached

TOOL_NAME = "pick_best_resume"


def _tool_schema(filenames: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "chosen_filename": {
                "type": "string", "enum": filenames,
                "description": "The filename of the resume that best fits the job description.",
            },
            "reason": {
                "type": "string",
                "description": (
                    "One sentence grounded in what's actually written: name the specific skills, "
                    "projects, or experience in the chosen resume that match the job description. "
                    "Never invent requirements or resume content that isn't there."
                ),
            },
        },
        "required": ["chosen_filename", "reason"],
    }


_SYSTEM = (
    "You are matching a student's resumes on file against a job description extracted from a "
    "recruiter email. Pick the ONE resume whose actual written content — skills, projects, "
    "experience — best fits the job description. Call pick_best_resume with your choice."
)


def _user_text(jd_text: str, resumes: list[tuple[str, str]]) -> str:
    parts = [f"JOB DESCRIPTION:\n{jd_text.strip()[:6000]}"]
    for name, text in resumes:
        parts.append(f"--- RESUME FILE: {name} ---\n{text.strip()[:4000]}")
    return "\n\n".join(parts)


def _call_anthropic(client, model: str, system: str, text: str, filenames: list[str]) -> dict:
    tool = {
        "name": TOOL_NAME,
        "description": "Record which resume on file best fits this job description.",
        "input_schema": _tool_schema(filenames),
    }
    resp = client.messages.create(
        model=model, max_tokens=512, temperature=0, system=system,
        tools=[tool], tool_choice={"type": "tool", "name": TOOL_NAME},
        messages=[{"role": "user", "content": text}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == TOOL_NAME:
            return block.input
    raise ValueError("model response had no pick_best_resume tool_use block")


def _call_openai_compatible(client, model: str, system: str, text: str, filenames: list[str]) -> dict:
    tool = {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": "Record which resume on file best fits this job description.",
            "parameters": _tool_schema(filenames),
        },
    }
    resp = client.chat.completions.create(
        model=model, temperature=0, max_tokens=512,
        tools=[tool], tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": text}],
    )
    message = resp.choices[0].message
    for call in (getattr(message, "tool_calls", None) or []):
        if call.function.name == TOOL_NAME:
            return json.loads(call.function.arguments)
    raise ValueError("model response had no pick_best_resume tool call")


def select_best_resume(
    jd_text: str,
    resumes: list[tuple[str, str]],  # (filename, extracted_text), non-empty
    *, api_key: str, model: str, provider: str, base_url: str | None, db_path: str, use_cache: bool = True,
) -> tuple[str, str]:
    """Returns (chosen_filename, reason). Raises on any failure — caller
    (resume.select_resume_smart) catches and falls back."""
    filenames = [name for name, _ in resumes]
    user_text = _user_text(jd_text, resumes)
    phash = prompt_hash(provider, model, _SYSTEM, user_text)

    if use_cache:
        cached = get_cached(db_path, phash)
        if cached is not None:
            return cached["chosen_filename"], cached["reason"]

    client = get_client(provider, api_key, base_url)
    call_fn = _call_anthropic if provider == "anthropic" else _call_openai_compatible
    result = call_with_rate_limit_backoff(lambda: call_fn(client, model, _SYSTEM, user_text, filenames))

    if result.get("chosen_filename") not in filenames:
        raise ValueError(f"model picked an unknown filename: {result.get('chosen_filename')!r}")

    if use_cache:
        set_cached(db_path, phash, model, result)
    return result["chosen_filename"], result["reason"]

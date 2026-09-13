"""Generates a JD-tailored resume from a student's master profile, via forced
tool use — same shape as resume_match.py's pick_best_resume call. Grounding
is enforced at the SCHEMA level, not just by instruction: `skills` and each
project `title` are constrained by JSON-schema `enum` to literally-present
master-profile entries, exactly like resume_match.py already constrains
`chosen_filename` — so the model cannot select or invent something that
isn't there, only choose which real things to emphasize and how to phrase
them. This is a read, never ledgered, and purely optional: resume.py catches
any failure here and falls back to a different resume-selection tier
(exactly which one depends on resume_mode — see select_resume_smart's own
docstring), so a bad or failed generation never blocks planning."""
from __future__ import annotations

import json

from cutoff.llm.client import call_with_rate_limit_backoff, get_cached, get_client, prompt_hash, set_cached
from cutoff.models import MasterProfile
from cutoff.pipeline.master_profile import format_for_prompt

TOOL_NAME = "generate_tailored_resume"


def _tool_schema(skills: list[str], project_titles: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "headline": {
                "type": "string",
                "description": (
                    "A 2-3 sentence Professional Summary (like the opening paragraph of a real resume), "
                    "positioning the student for this specific job description. Grounded ONLY in what's "
                    "in the master profile below — describe the student's real background/specialization "
                    "and what they're seeking, using their actual skills and domains, never a claim or "
                    "qualification that isn't supported by the master profile."
                ),
            },
            "skills": {
                "type": "array",
                "items": {"type": "string", "enum": skills} if skills else {"type": "string"},
                "description": (
                    "Skills from the master profile most relevant to this job description, in priority "
                    "order. Must be chosen only from the exact list of skills provided — never a skill "
                    "that isn't in that list."
                ),
            },
            "highlighted_projects": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": (
                            {"type": "string", "enum": project_titles} if project_titles
                            else {"type": "string"}
                        ),
                        "bullets": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["title", "bullets"],
                },
                "description": (
                    "2-4 projects from the master profile most relevant to this job description. `title` "
                    "must be exactly one of the project titles provided. `bullets` may rephrase the "
                    "project's own bullets to mirror the job description's terminology, but must not "
                    "introduce a claim, metric, or fact that isn't in the original bullets for that project."
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
    "profile below as source material. Select and rephrase the skills and projects that best match "
    "this job description's own terminology. You may ONLY select skills and project titles that are "
    "listed in the master profile — never invent one. You may rephrase a project's bullets to mirror "
    "the job description's terminology, but never add a claim, metric, or fact that isn't already in "
    f"that project's own bullets. Call {TOOL_NAME} with your result."
)


def _user_text(jd_text: str, profile: MasterProfile) -> str:
    return (
        f"JOB DESCRIPTION:\n{jd_text.strip()[:6000]}\n\n"
        f"MASTER PROFILE (the only source of truth — select and rephrase only from here):\n"
        f"{format_for_prompt(profile)[:12000]}"
    )


def _call_anthropic(client, model: str, system: str, text: str, skills: list[str], project_titles: list[str]) -> dict:
    tool = {
        "name": TOOL_NAME,
        "description": "Record a job-description-tailored resume built only from the master profile.",
        "input_schema": _tool_schema(skills, project_titles),
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


def _call_openai_compatible(client, model: str, system: str, text: str, skills: list[str], project_titles: list[str]) -> dict:
    tool = {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": "Record a job-description-tailored resume built only from the master profile.",
            "parameters": _tool_schema(skills, project_titles),
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


def _validate(result: dict, profile: MasterProfile) -> dict:
    """Defense in depth: the JSON schema's `enum` already constrains the
    provider's own output, but providers don't always enforce enum
    constraints strictly (found live elsewhere in this codebase, hence
    resume_match.py's own chosen_filename re-check) — re-verify in Python too."""
    allowed_skills = {s.lower() for s in profile.skills}
    for skill in result.get("skills") or []:
        if skill.lower() not in allowed_skills:
            raise ValueError(f"model returned a skill not in the master profile: {skill!r}")

    projects_by_title = {p.title.lower(): p for p in profile.projects}
    if not result.get("highlighted_projects"):
        raise ValueError("model returned no highlighted_projects")
    for project in result["highlighted_projects"]:
        title = project.get("title", "")
        if title.lower() not in projects_by_title:
            raise ValueError(f"model returned a project title not in the master profile: {title!r}")
        if not project.get("bullets"):
            raise ValueError(f"malformed project entry (no bullets): {project!r}")
    return result


def generate_tailored_resume(
    jd_text: str, profile: MasterProfile,
    *, api_key: str, model: str, provider: str, base_url: str | None, db_path: str, use_cache: bool = True,
) -> tuple[dict, str]:
    """Returns (sections, match_reason). Raises on any failure — caller
    (resume.select_resume_smart) catches and falls back to a different
    resume-selection tier, exactly which one depending on resume_mode."""
    user_text = _user_text(jd_text, profile)
    phash = prompt_hash(provider, model, _SYSTEM, user_text)

    if use_cache:
        cached = get_cached(db_path, phash)
        if cached is not None:
            return cached, cached["match_reason"]

    client = get_client(provider, api_key, base_url)
    call_fn = _call_anthropic if provider == "anthropic" else _call_openai_compatible
    project_titles = [p.title for p in profile.projects]
    result = _validate(
        call_with_rate_limit_backoff(lambda: call_fn(client, model, _SYSTEM, user_text, profile.skills, project_titles)),
        profile,
    )

    if use_cache:
        set_cached(db_path, phash, model, result)
    return result, result["match_reason"]

"""Drafts short answers for a form's open-ended text questions (Section 6.5
extension) — forced tool use, same shape as extract.py's record_notice call.
Grounded ONLY in the chosen resume's own text and this drive's job
description text, both already extracted elsewhere in the pipeline; told
explicitly to return an empty string for anything it can't honestly answer
from those two sources rather than invent a fact. Unlike extraction, there
is no near-verbatim grounding validator for generated prose — that's why
these are drafts the student reviews and edits before submitting anything
themselves (Section 6.5's guarantee is unchanged), not a second automated
safety net."""
from __future__ import annotations

import json

from cutoff.llm.client import call_with_rate_limit_backoff, get_client

TOOL_NAME = "answer_form_questions"

# Found live: using each question's own raw text as a JSON Schema *property
# name* (e.g. "Why ould we hire you?", full of spaces/punctuation) made
# Gemini return no tool call at all — property keys need to look like plain
# identifiers, not freeform text. Synthetic "qN" keys sidestep that; the
# real question text still reaches the model, just as each property's
# description instead of its name.
def _question_key(index: int) -> str:
    return f"q{index}"


def _tool_schema(titles: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            _question_key(i): {
                "type": "string",
                "description": (
                    f'Answer to: "{title}". A concise, grounded draft answer (1-3 sentences for fit/motivation questions, '
                    "or a factual answer/list for skills, links, or facts) based on the resume and job description below. "
                    "Synthesize genuine fit from the candidate's actual projects and skills; never invent qualifications. "
                    "Only return empty string if completely unrelated to their background."
                ),
            }
            for i, title in enumerate(titles)
        },
        "required": [_question_key(i) for i in range(len(titles))],
    }


_SYSTEM = (
    "You are drafting short answers to a job application form's questions, on behalf of a "
    "student, using only their resume and the job description below. These are drafts the student will "
    "review and edit before submitting anything themselves.\n\n"
    "Instructions:\n"
    "1. For questions asking why the company should hire the student, why they want to join, or what makes "
    "them a great fit (e.g. 'Why should we hire you?', 'Why this company?', 'Tell us about yourself'), "
    "synthesize a concise, persuasive 1-3 sentence pitch connecting their genuine technical skills and projects "
    "from the resume to the requirements of the job description. Do NOT leave these blank.\n"
    "2. For skill or tech stack questions, list their relevant technical skills from the resume (comma-separated "
    "if requested).\n"
    "3. For factual questions (phone number, profile links, locations), provide the exact value from the resume.\n"
    "4. Ground all claims in what is actually written in the resume — never invent companies, projects, or degrees.\n"
    "5. Only return an empty string if a question cannot be reasonably addressed from their resume background.\n"
    "Call answer_form_questions with one entry per question."
)


def _user_text(resume_text: str, jd_text: str, titles: list[str]) -> str:
    parts = [
        f"RESUME:\n{resume_text.strip()[:6000]}",
        f"JOB DESCRIPTION:\n{jd_text.strip()[:4000]}" if jd_text.strip() else "JOB DESCRIPTION: (not available)",
        "QUESTIONS:\n" + "\n".join(f"{_question_key(i)}: {t}" for i, t in enumerate(titles)),
    ]
    return "\n\n".join(parts)


def _call_anthropic(client, model: str, system: str, text: str, titles: list[str]) -> dict:
    tool = {
        "name": TOOL_NAME,
        "description": "Record a draft answer for each form question.",
        "input_schema": _tool_schema(titles),
    }
    resp = client.messages.create(
        model=model, max_tokens=1024, temperature=0, system=system,
        tools=[tool], tool_choice={"type": "tool", "name": TOOL_NAME},
        messages=[{"role": "user", "content": text}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == TOOL_NAME:
            return block.input
    raise ValueError("model response had no answer_form_questions tool_use block")


def _call_openai_compatible(client, model: str, system: str, text: str, titles: list[str]) -> dict:
    tool = {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": "Record a draft answer for each form question.",
            "parameters": _tool_schema(titles),
        },
    }
    resp = client.chat.completions.create(
        model=model, temperature=0, max_tokens=1024,
        tools=[tool], tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": text}],
    )
    message = resp.choices[0].message
    for call in (getattr(message, "tool_calls", None) or []):
        if call.function.name == TOOL_NAME:
            return json.loads(call.function.arguments)
    raise ValueError("model response had no answer_form_questions tool call")


def draft_answers(
    resume_text: str, jd_text: str, question_titles: list[str],
    *, api_key: str, model: str, provider: str, base_url: str | None,
) -> dict[str, str]:
    """Returns {question_title: answer}, answer "" wherever the model
    couldn't ground one. Raises on any API failure — caller (form_autofill)
    catches it and just leaves those questions unfilled."""
    if not question_titles:
        return {}
    client = get_client(provider, api_key, base_url)
    call_fn = _call_anthropic if provider == "anthropic" else _call_openai_compatible
    text = _user_text(resume_text, jd_text, question_titles)
    result = call_with_rate_limit_backoff(lambda: call_fn(client, model, _SYSTEM, text, question_titles))
    return {title: str(result.get(_question_key(i), "")) for i, title in enumerate(question_titles)}

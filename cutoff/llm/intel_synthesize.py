"""Distills interview-prep web-search snippets into a short, grounded
strategy summary (new extension) — forced tool-use, same discipline as
resume_generate.py/profile_extract.py: the LLM may only summarize what's
actually in the provided snippets, never invent a past interview question
or coding-round detail the snippets don't support. `top_reference_links`
is enum-constrained to the literal search-result URLs, same schema-level
grounding as resume_generate.py's project titles. This is a read, never
ledgered, and purely optional — cutoff.pipeline.prep_intel catches any
failure here and treats it as "nothing to show," never blocking planning."""
from __future__ import annotations

import json

from cutoff.llm.client import call_with_rate_limit_backoff, get_client

TOOL_NAME = "extract_prep_intel"


def _tool_schema(urls: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "strategy_summary": {
                "type": "string",
                "description": (
                    "At most 2 sentences summarizing frequently tested concepts, grounded ONLY in the "
                    "provided search snippets (e.g. 'Historically, Solstice focuses heavily on Graph "
                    "traversals and SQL window functions. Expect a medium-level DP problem in the OA.'). "
                    "If the snippets don't mention specific technical details, give a short, generic "
                    "behavioral-interview-prep sentence instead — never invent a past coding question or "
                    "round structure the snippets don't actually support."
                ),
            },
            "top_reference_links": {
                "type": "array",
                "items": {"type": "string", "enum": urls} if urls else {"type": "string"},
                "minItems": 1,
                "maxItems": 2,
                "description": "1-2 of the provided URLs offering the best full interview-experience write-ups.",
            },
        },
        "required": ["strategy_summary", "top_reference_links"],
    }


_SYSTEM = (
    "You are summarizing interview-preparation intel for a student from web-search snippets about a "
    "specific company and role. Base your summary ONLY on the provided snippets — never invent a past "
    "interview question, coding round, or technical detail the snippets don't actually mention. If the "
    "snippets lack specific technical content, give a short, generic behavioral-interview-prep sentence "
    "instead of guessing. Only select reference links from the URLs provided. "
    f"Call {TOOL_NAME} with your result."
)


def _user_text(company: str, role: str, search_results: list[dict]) -> str:
    parts = [f"COMPANY: {company}\nROLE: {role}\n\nSEARCH RESULTS:"]
    for r in search_results:
        parts.append(f"- {r['title']}\n  {r['snippet']}\n  URL: {r['url']}")
    return "\n".join(parts)


def _call_anthropic(client, model: str, text: str, urls: list[str]) -> dict:
    tool = {"name": TOOL_NAME, "description": "Record the distilled interview-prep intel.",
            "input_schema": _tool_schema(urls)}
    resp = client.messages.create(
        model=model, max_tokens=512, temperature=0, system=_SYSTEM,
        tools=[tool], tool_choice={"type": "tool", "name": TOOL_NAME},
        messages=[{"role": "user", "content": text}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == TOOL_NAME:
            return block.input
    raise ValueError(f"model response had no {TOOL_NAME} tool_use block")


def _call_openai_compatible(client, model: str, text: str, urls: list[str]) -> dict:
    tool = {"type": "function", "function": {
        "name": TOOL_NAME, "description": "Record the distilled interview-prep intel.",
        "parameters": _tool_schema(urls),
    }}
    resp = client.chat.completions.create(
        model=model, temperature=0, max_tokens=512,
        tools=[tool], tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
        messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": text}],
    )
    message = resp.choices[0].message
    for call in (getattr(message, "tool_calls", None) or []):
        if call.function.name == TOOL_NAME:
            return json.loads(call.function.arguments)
    raise ValueError(f"model response had no {TOOL_NAME} tool call")


def _validate(result: dict, urls: list[str]) -> dict:
    valid_urls = set(urls)
    links = result.get("top_reference_links") or []
    if not links:
        raise ValueError("model returned no reference links")
    for link in links:
        if link not in valid_urls:
            raise ValueError(f"model returned a link not in the search results: {link!r}")
    return result


def synthesize_prep_strategy(
    company: str, role: str, search_results: list[dict],
    *, api_key: str, model: str, provider: str, base_url: str | None,
) -> dict | None:
    """Returns {"strategy_summary": str, "top_reference_links": [str, ...]}.
    Returns None immediately if there's nothing to summarize. Raises on any
    other failure (LLM error, malformed/ungrounded response) — the caller
    (cutoff.pipeline.prep_intel) catches and treats that as "nothing to
    show," same never-block-planning guarantee as everywhere else."""
    if not search_results:
        return None

    urls = [r["url"] for r in search_results if r.get("url")]
    user_text = _user_text(company, role, search_results)
    client = get_client(provider, api_key, base_url)
    call_fn = _call_anthropic if provider == "anthropic" else _call_openai_compatible
    return _validate(call_with_rate_limit_backoff(lambda: call_fn(client, model, user_text, urls)), urls)

"""The one LLM job: email -> Notice, via forced tool use (Section 9). This
calls the real LLM API — never call it from a unit test; `run.py` takes
`extract_fn` as a parameter so tests can inject a stub.

Supports three providers (see `cutoff.llm.client`): "anthropic" (Messages
API, `tool_use` content blocks) and "openrouter"/"gemini" (OpenAI-compatible
chat completions, `tool_calls[].function.arguments` as a JSON string) —
the latter two share the same call shape, just a different base_url/key."""
from __future__ import annotations

import json

from cutoff.llm import prompts
from cutoff.llm.client import call_with_rate_limit_backoff, get_cached, get_client, is_rate_limited, prompt_hash, set_cached
from cutoff.models import EmailMessage, Notice

EXTRACTION_FAILED_SENTINEL = "__extraction_failed__"


def _call_anthropic(client, model: str, system: str, text: str) -> dict:
    tool = {
        "name": prompts.TOOL_NAME,
        "description": "Record the notice extracted from this email.",
        "input_schema": prompts.notice_tool_schema(),
    }
    resp = client.messages.create(
        model=model, max_tokens=2048, temperature=0, system=system,
        tools=[tool], tool_choice={"type": "tool", "name": prompts.TOOL_NAME},
        messages=[{"role": "user", "content": text}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == prompts.TOOL_NAME:
            return block.input
    raise ValueError("model response had no record_notice tool_use block")


def _call_openai_compatible(client, model: str, system: str, text: str) -> dict:
    tool = {
        "type": "function",
        "function": {
            "name": prompts.TOOL_NAME,
            "description": "Record the notice extracted from this email.",
            "parameters": prompts.notice_tool_schema(),
        },
    }
    resp = client.chat.completions.create(
        model=model, temperature=0, max_tokens=2048,
        tools=[tool], tool_choice={"type": "function", "function": {"name": prompts.TOOL_NAME}},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": text}],
    )
    message = resp.choices[0].message
    tool_calls = getattr(message, "tool_calls", None) or []
    for call in tool_calls:
        if call.function.name == prompts.TOOL_NAME:
            return json.loads(call.function.arguments)
    raise ValueError("model response had no record_notice tool call")


def extract_notice(
    msg: EmailMessage,
    body_clean: str,
    quoted_history: str,
    attachment_text: str,
    *,
    api_key: str,
    model: str,
    db_path: str,
    timezone_name: str,
    use_cache: bool = True,
    provider: str = "anthropic",
    base_url: str | None = None,
) -> Notice:
    system = prompts.build_system_prompt(msg.received_at.isoformat(), timezone_name)
    user_text = prompts.build_user_text(msg, body_clean, quoted_history, attachment_text)
    phash = prompt_hash(provider, model, system, user_text)

    if use_cache:
        cached = get_cached(db_path, phash)
        if cached is not None:
            return Notice.model_validate(cached)

    client = get_client(provider, api_key, base_url)
    call_fn = _call_anthropic if provider == "anthropic" else _call_openai_compatible

    def call(extra_text: str = "") -> Notice:
        text = user_text if not extra_text else f"{user_text}\n\n{extra_text}"
        return Notice.model_validate(call_fn(client, model, system, text))

    try:
        notice = call_with_rate_limit_backoff(call)
    except Exception as first_error:
        if is_rate_limited(first_error):
            # Backoff already exhausted MAX_RATE_LIMIT_RETRIES above; a
            # "fix your response" retry would just hit the same cap again.
            notice = Notice(notice_type="NON_DRIVE", unverified_fields=[EXTRACTION_FAILED_SENTINEL])
        else:
            try:
                fix_prompt = f"Your previous response was invalid: {first_error}. Call record_notice again, fixing it."
                notice = call_with_rate_limit_backoff(lambda: call(fix_prompt))
            except Exception:
                notice = Notice(notice_type="NON_DRIVE", unverified_fields=[EXTRACTION_FAILED_SENTINEL])

    if use_cache:
        set_cached(db_path, phash, model, notice.model_dump(mode="json"))
    return notice

"""LLM client + llm_cache (Section 9: cache by sha256(provider+model+system+user)).

Three providers are supported:
- "anthropic": the official `anthropic` SDK, talking to Anthropic's Messages API
  directly (the spec's default — Section 9).
- "openrouter" / "gemini": the `openai` SDK pointed at an OpenAI-compatible
  endpoint (OpenRouter's own, or Google's Gemini OpenAI-compatibility layer),
  for when no Anthropic key is available. Tool-calling shape differs between
  Anthropic and OpenAI-compatible APIs (`tool_use` blocks vs
  `tool_calls[].function.arguments`), so `extract.py` branches on provider,
  treating "openrouter" and "gemini" identically past the client itself.
"""
from __future__ import annotations

import hashlib
import json
import random
import threading
import time

from cutoff import db

_clients: dict[tuple[str, str, str], object] = {}

MAX_RATE_LIMIT_RETRIES = 3

# A free-tier RPM cap (found live: 15 req/min) means a *burst* of calls —
# every message from one poll, or the eval harness's 30 scenarios back to
# back — can blow through the limit before any single call ever gets a 429
# to retry against, since by then several more calls are already in flight.
# Pacing every call process-wide is a no-op for normal production traffic
# (real emails arrive far slower than one per 4s), but is what actually
# keeps a burst under the cap instead of just backing off after the fact.
MIN_CALL_INTERVAL_SECONDS = 4.1  # a hair over 60/15, so 15 calls/min never trips the cap
_pacing_lock = threading.Lock()
_last_call_at = 0.0


def _pace() -> None:
    global _last_call_at
    with _pacing_lock:
        wait = _last_call_at + MIN_CALL_INTERVAL_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


def is_rate_limited(error: Exception) -> bool:
    return getattr(error, "status_code", None) == 429


def call_with_rate_limit_backoff(call):
    """A 429 isn't the model's fault — retrying with "fix your response"
    wording makes no sense for it, and a free-tier RPM cap (found live: 15
    req/min) can make a plain single retry land in the same throttled
    window. Paces every call first (see _pace) so a burst doesn't create the
    429s in the first place, then backs off (exponential + jitter) and
    retries a few times before giving up. Shared by extract.py and
    resume_match.py — both make forced tool-use calls against the same
    three providers."""
    attempt = 0
    while True:
        _pace()
        try:
            return call()
        except Exception as e:
            if not is_rate_limited(e) or attempt >= MAX_RATE_LIMIT_RETRIES:
                raise
            attempt += 1
            time.sleep(min(2 ** attempt, 10) + random.uniform(0, 0.5))


def get_client(provider: str, api_key: str, base_url: str | None = None):
    key = (provider, api_key, base_url or "")
    client = _clients.get(key)
    if client is not None:
        return client

    if provider == "anthropic":
        from anthropic import Anthropic

        client = Anthropic(api_key=api_key)
    elif provider == "openrouter":
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url or "https://openrouter.ai/api/v1")
    elif provider == "gemini":
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url or "https://generativelanguage.googleapis.com/v1beta/openai/")
    else:
        raise ValueError(f"unknown LLM provider: {provider!r}")

    _clients[key] = client
    return client


def prompt_hash(provider: str, model: str, system: str, user_text: str) -> str:
    payload = json.dumps(
        {"provider": provider, "model": model, "system": system, "user": user_text}, sort_keys=True
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def get_cached(db_path: str, prompt_hash_value: str) -> dict | None:
    conn = db.get_connection(db_path)
    row = conn.execute("SELECT response FROM llm_cache WHERE prompt_hash = ?", (prompt_hash_value,)).fetchone()
    return json.loads(row["response"]) if row else None


def set_cached(db_path: str, prompt_hash_value: str, model: str, response: dict) -> None:
    conn = db.get_connection(db_path)
    conn.execute(
        "INSERT INTO llm_cache (prompt_hash, model, response) VALUES (?, ?, ?) "
        "ON CONFLICT(prompt_hash) DO UPDATE SET response = excluded.response",
        (prompt_hash_value, model, json.dumps(response)),
    )
    conn.commit()

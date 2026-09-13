"""cutoff.llm.client's rate-limit backoff (Section 0 rule 3: no real API
calls in tests — this exercises the retry logic in isolation with a fake
call function and fake exceptions carrying a `status_code`, the same
attribute both the anthropic and openai SDKs' real exceptions expose).

Found live (Phase 4): a free-tier RPM cap (15 req/min) made a single
"fix your response" retry land in the same throttled window, and sending
that wording for a 429 makes no sense anyway — it's not the model's fault.
Lives in client.py (not extract.py) since resume_match.py's forced tool-use
calls need the exact same backoff."""
import pytest

from cutoff.llm import client
from cutoff.llm.client import MAX_RATE_LIMIT_RETRIES, call_with_rate_limit_backoff, is_rate_limited


class _FakeRateLimitError(Exception):
    status_code = 429


class _FakeValidationError(Exception):
    status_code = None


@pytest.fixture(autouse=True)
def _reset_pacing():
    """call_with_rate_limit_backoff paces every call against a module-level
    "last call" timestamp (Section 0 rule 3 note: found live, a free-tier
    RPM cap needs pacing *before* a burst causes 429s, not just backoff
    after). That timestamp is process-global, so back-to-back tests in this
    file would otherwise see a stale recent value and actually sleep —
    reset it so each test starts as if no call has ever been made."""
    client._last_call_at = 0.0


def test_is_rate_limited_detects_429():
    assert is_rate_limited(_FakeRateLimitError())
    assert not is_rate_limited(_FakeValidationError())
    assert not is_rate_limited(ValueError("no status_code attribute at all"))


def test_backoff_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr("cutoff.llm.client.time.sleep", lambda _seconds: None)
    attempts = {"n": 0}

    def flaky_call():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _FakeRateLimitError()
        return "ok"

    result = call_with_rate_limit_backoff(flaky_call)
    assert result == "ok"
    assert attempts["n"] == 3


def test_backoff_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr("cutoff.llm.client.time.sleep", lambda _seconds: None)
    attempts = {"n": 0}

    def always_rate_limited():
        attempts["n"] += 1
        raise _FakeRateLimitError()

    with pytest.raises(_FakeRateLimitError):
        call_with_rate_limit_backoff(always_rate_limited)
    assert attempts["n"] == MAX_RATE_LIMIT_RETRIES + 1  # initial attempt + retries


def test_backoff_never_retries_a_non_rate_limit_error(monkeypatch):
    monkeypatch.setattr("cutoff.llm.client.time.sleep", lambda _seconds: (_ for _ in ()).throw(
        AssertionError("should never sleep/retry for a non-429 error")
    ))
    attempts = {"n": 0}

    def broken_call():
        attempts["n"] += 1
        raise _FakeValidationError("malformed tool call")

    with pytest.raises(_FakeValidationError):
        call_with_rate_limit_backoff(broken_call)
    assert attempts["n"] == 1


def test_paces_back_to_back_calls_below_the_rpm_cap(monkeypatch):
    # Found live: a burst of calls (30 eval scenarios, or every message from
    # one poll) can blow through a free-tier RPM cap before any single call
    # ever gets a 429 to back off against. Pacing every call keeps a burst
    # under the cap in the first place. Fake a clock instead of sleeping for
    # real, and assert the second call waits for roughly a full interval.
    fake_now = {"t": 1000.0}
    monkeypatch.setattr("cutoff.llm.client.time.monotonic", lambda: fake_now["t"])
    waits = []
    monkeypatch.setattr("cutoff.llm.client.time.sleep", lambda seconds: waits.append(seconds))

    call_with_rate_limit_backoff(lambda: "ok")  # first call: nothing to pace against yet
    assert waits == []

    fake_now["t"] += 0.1  # second call arrives almost immediately after
    call_with_rate_limit_backoff(lambda: "ok")
    assert len(waits) == 1
    assert waits[0] == pytest.approx(client.MIN_CALL_INTERVAL_SECONDS - 0.1, abs=0.01)

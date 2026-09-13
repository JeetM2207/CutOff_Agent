"""CircuitBreaker.snapshot() and executor.shared_breaker() (Section 16
extension: the dashboard's health panel reads real circuit state, not a
fabricated demo number). Regression coverage for a live finding: main.py's
worker loop never passed a breaker into run_pending, so its `breaker or
CircuitBreaker()` default silently created a brand-new, empty breaker every
single poll — a circuit only ever protected one batch of actions, never
remembered a run of failures across polls."""
from datetime import datetime, timezone

from cutoff.pipeline import executor
from cutoff.pipeline.executor import CIRCUIT_FAILURE_THRESHOLD, CircuitBreaker


def test_snapshot_is_empty_before_any_activity():
    breaker = CircuitBreaker()
    assert breaker.snapshot() == {}


def test_snapshot_reflects_failures_without_opening_below_threshold():
    breaker = CircuitBreaker()
    now = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)
    state = breaker.state_for("sheets")
    state.record_failure(now)

    # Found live: this used to call snapshot() with no `now`, so "open"
    # depended on how much real wall-clock time had passed since `now`
    # above — it passed for the first few hours of this session and then
    # started failing on its own, with no code change, once real time
    # moved past the hardcoded window. `now` is injected explicitly here so
    # the assertion can never depend on when the suite happens to run.
    snap = breaker.snapshot(now)
    assert snap["sheets"]["consecutive_failures"] == 1
    assert snap["sheets"]["open"] is False
    assert snap["sheets"]["opened_until"] is None


def test_snapshot_shows_open_once_threshold_is_hit():
    breaker = CircuitBreaker()
    now = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)
    state = breaker.state_for("calendar")
    for _ in range(CIRCUIT_FAILURE_THRESHOLD):
        state.record_failure(now)

    snap = breaker.snapshot(now)  # still well within the open window at this same instant
    assert snap["calendar"]["open"] is True
    assert snap["calendar"]["opened_until"] is not None


def test_snapshot_never_mutates_state():
    breaker = CircuitBreaker()
    now = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)
    breaker.state_for("telegram").record_failure(now)
    breaker.snapshot()
    breaker.snapshot()
    assert breaker.state_for("telegram").consecutive_failures == 1


def test_shared_breaker_returns_same_instance_for_same_db_path():
    b1 = executor.shared_breaker("some/test/path.db")
    b2 = executor.shared_breaker("some/test/path.db")
    assert b1 is b2


def test_shared_breaker_returns_different_instances_for_different_db_paths():
    b1 = executor.shared_breaker("path-a.db")
    b2 = executor.shared_breaker("path-b.db")
    assert b1 is not b2

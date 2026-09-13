"""CalendarSource.upsert_event (Section 6.3) against a mocked googleapiclient
service — no real network. Regression test for a live bug: a 409 on insert
used to surface as CalendarConflict for the executor to "get, then retry
insert", but retrying insert on an id that already exists just 409s again,
uncaught — crashing run_pending every poll. upsert_event must self-heal by
patching in place instead."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError

from cutoff.adapters.calendar_real import CalendarSource
from cutoff.models import CalendarEvent
from cutoff.pipeline.executor import AdapterError


class _FakeExecute:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return self._result


class _FakeEvents:
    def __init__(self):
        self.insert_calls: list[dict] = []
        self.patch_calls: list[dict] = []
        self.insert_error: Exception | None = None

    def insert(self, calendarId, body):
        self.insert_calls.append({"calendarId": calendarId, "body": body})
        if self.insert_error is not None:
            return _FakeExecute(error=self.insert_error)
        return _FakeExecute(result={})

    def patch(self, calendarId, eventId, body):
        self.patch_calls.append({"calendarId": calendarId, "eventId": eventId, "body": body})
        return _FakeExecute(result={})


class _FakeService:
    def __init__(self, events: _FakeEvents):
        self._events = events

    def events(self):
        return self._events


def _event() -> CalendarEvent:
    return CalendarEvent(
        event_id="unset", title="Northwind — deadline",
        start=datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc), end=datetime(2026, 9, 20, 18, 30, tzinfo=timezone.utc),
    )


def test_upsert_event_inserts_when_no_conflict():
    events = _FakeEvents()
    src = CalendarSource(_FakeService(events), "primary")

    result = src.upsert_event("evt1", _event())

    assert result == "evt1"
    assert len(events.insert_calls) == 1
    assert events.patch_calls == []


def test_upsert_event_self_heals_by_patching_on_409():
    events = _FakeEvents()
    events.insert_error = HttpError(resp=SimpleNamespace(status=409, reason="Conflict"), content=b"{}")
    src = CalendarSource(_FakeService(events), "primary")

    result = src.upsert_event("evt1", _event())

    assert result == "evt1"
    assert len(events.insert_calls) == 1
    assert len(events.patch_calls) == 1
    assert events.patch_calls[0]["eventId"] == "evt1"


def test_upsert_event_translates_non_409_errors_to_adapter_error_without_patching():
    # Found live: a real Gmail/Sheets/Drive 429 or 5xx propagated as a raw
    # HttpError, which with_retry() (only catches AdapterError) never saw —
    # so the retry/backoff/circuit-breaker system did nothing for real
    # traffic. A non-409 here must become AdapterError (with status_code
    # preserved for with_retry's retryability check), not stay an HttpError.
    events = _FakeEvents()
    events.insert_error = HttpError(resp=SimpleNamespace(status=500, reason="Internal Error"), content=b"{}")
    src = CalendarSource(_FakeService(events), "primary")

    with pytest.raises(AdapterError) as exc_info:
        src.upsert_event("evt1", _event())
    assert exc_info.value.status_code == 500

    assert events.patch_calls == []

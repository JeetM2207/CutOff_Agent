"""GmailSource.list_new (Section 6.1) against a mocked googleapiclient
service — no real network. Regression test for a live bug: an auto-saved
draft matches a `from:` search just like a real sent message, but its
content is whatever was typed so far (no attachment yet, incomplete text) —
the LLM reasonably read one as suspicious. Drafts are now excluded from the
search entirely.

Also regression-tests a second live bug: a real HttpError from Gmail used to
propagate raw, which with_retry() (only catches AdapterError) never saw — so
a real 429/500 got none of the retry/backoff/circuit-breaker treatment the
eval harness's simulated chaos gets. Found while debugging an unrelated
Drive crash; telegram_real.py already did this translation correctly,
gmail_real.py never did."""
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError

from cutoff.adapters.gmail_real import GmailSource
from cutoff.pipeline.executor import AdapterError


class _FakeExecute:
    def __init__(self, result=None, error=None):
        self._result = result or {}
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return self._result


class _FakeMessages:
    def __init__(self):
        self.list_queries: list[str] = []
        self.list_error: Exception | None = None

    def list(self, userId, q, pageToken=None):
        self.list_queries.append(q)
        if self.list_error is not None:
            return _FakeExecute(error=self.list_error)
        return _FakeExecute({"messages": []})


class _FakeUsers:
    def __init__(self, messages: _FakeMessages):
        self._messages = messages

    def messages(self):
        return self._messages


class _FakeService:
    def __init__(self):
        self.messages = _FakeMessages()

    def users(self):
        return _FakeUsers(self.messages)


def test_list_new_query_excludes_drafts_with_senders():
    service = _FakeService()
    src = GmailSource(service)

    src.list_new(["careers.demo.college@gmail.com", "jeetmanseta71@gmail.com"], None)

    query = service.messages.list_queries[0]
    assert "-in:drafts" in query
    assert "from:careers.demo.college@gmail.com" in query
    assert "from:jeetmanseta71@gmail.com" in query


def test_list_new_query_excludes_drafts_with_no_senders():
    service = _FakeService()
    src = GmailSource(service)

    src.list_new([], None)

    query = service.messages.list_queries[0]
    assert "-in:drafts" in query
    assert "newer_than:30d" in query


def test_list_new_translates_http_error_to_adapter_error():
    service = _FakeService()
    service.messages.list_error = HttpError(resp=SimpleNamespace(status=429, reason="Too Many Requests"), content=b"{}")
    src = GmailSource(service)

    with pytest.raises(AdapterError) as exc_info:
        src.list_new(["careers.demo.college@gmail.com"], None)
    assert exc_info.value.status_code == 429

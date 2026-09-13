"""FaultInjector wraps any adapter (real or fake) to inject faults by rule
(Section 15.4). Used only by the eval harness and chaos tests — never wired
into normal pipeline operation."""
from __future__ import annotations

import random

from cutoff.pipeline.executor import AdapterError


class FaultInjector:
    """Proxies every call to `target`; for methods named in `methods`, may
    raise an `AdapterError` first, according to `rules` (one profile's
    per-app dict from chaos_profiles.yaml, e.g.
    `{"http_429_first_n": 3, "retry_after_s": 1}`)."""

    def __init__(self, target, rules: dict, *, methods: tuple[str, ...]):
        self._target = target
        self._rules = rules
        self._methods = methods
        self._call_counts: dict[str, int] = {}
        self._rng = random.Random(rules.get("seed", 0))

    def __getattr__(self, name):
        attr = getattr(self._target, name)
        if name not in self._methods or not callable(attr):
            return attr

        def wrapped(*args, **kwargs):
            self._call_counts[name] = self._call_counts.get(name, 0) + 1
            self._maybe_raise(self._call_counts[name])
            return attr(*args, **kwargs)

        return wrapped

    def _maybe_raise(self, call_number: int) -> None:
        first_n = self._rules.get("http_429_first_n")
        if first_n and call_number <= first_n:
            raise AdapterError("injected 429", status_code=429, retry_after=self._rules.get("retry_after_s"))

        timeout_n = self._rules.get("timeout_first_n")
        if timeout_n and call_number <= timeout_n:
            raise AdapterError("injected timeout", status_code=None)

        rate = self._rules.get("http_500_rate")
        if rate and self._rng.random() < rate:
            raise AdapterError("injected 500", status_code=500)


class _DuplicatingMail:
    """gmail.duplicate_every_message (Section 15.4): `list_new` returns each
    ref twice, simulating duplicate delivery of the same message_id."""

    def __init__(self, inner):
        self._inner = inner

    def list_new(self, senders, since):
        refs = self._inner.list_new(senders, since)
        return [r for r in refs for _ in range(2)]

    def get(self, message_id):
        return self._inner.get(message_id)

    def get_attachment(self, message_id, attachment_id):
        return self._inner.get_attachment(message_id, attachment_id)

    def list_suspicious(self, keywords, since):
        return self._inner.list_suspicious(keywords, since)

    def deliver(self, msg, attachments=None):
        # Not part of the real MailSource protocol (FakeMailSource-only, used
        # by the eval harness to seed messages) — found live: this was
        # missing entirely, so the duplicate_delivery chaos profile crashed
        # with AttributeError on every single scenario, 0/31, and had never
        # actually been run and verified before.
        return self._inner.deliver(msg, attachments)


def wrap_mail(mail, rules: dict):
    if not rules:
        return mail
    wrapped = FaultInjector(mail, rules, methods=("list_new", "get", "get_attachment", "list_suspicious"))
    return _DuplicatingMail(wrapped) if rules.get("duplicate_every_message") else wrapped


def wrap_calendar(calendar, rules: dict):
    if not rules:
        return calendar
    return FaultInjector(calendar, rules, methods=("upsert_event", "cancel_event", "get_event", "list_exam_events"))


def wrap_telegram(messenger, rules: dict):
    if not rules:
        return messenger
    return FaultInjector(messenger, rules, methods=("send", "edit"))


def wrap_sheets(sheets, rules: dict):
    if not rules:
        return sheets
    return FaultInjector(sheets, rules, methods=("upsert_drive_row", "read_drive_row", "read_profile", "read_policy"))

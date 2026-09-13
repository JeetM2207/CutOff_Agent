"""Action ledger persistence + the executor loop (Section 11). Every side
effect goes through `plan_action` and is later carried out here — never
called directly from `run.py` or `planner.py`."""
from __future__ import annotations

import base64
import hashlib
import json
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cutoff import db, trace
from cutoff.adapters.base import CalendarStore, FileStore, Messenger, SheetStore
from cutoff.models import Action, Button, CalendarEvent

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
PERMANENT_STATUS_CODES = {400, 401, 403, 404}
MAX_ATTEMPTS = 5
LEASE_SECONDS = 30
CIRCUIT_FAILURE_THRESHOLD = 5
CIRCUIT_OPEN_SECONDS = 30


class AdapterError(Exception):
    """Raised by an adapter call; `status_code` drives retry classification."""

    def __init__(self, message: str, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class SimulatedCrash(Exception):
    """Raised by `run_pending` under the `crash_mid_run` chaos profile
    (Section 15.4) — the caller is expected to catch it, discard in-memory
    state (a fresh CircuitBreaker), and call `run_pending` again on the same
    SQLite file and the same (still-running) fake apps."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def event_id_for(key: str) -> str:
    """Deterministic, Calendar-API-compatible (base32hex) client-chosen event
    ID (Section 6.3) — re-planning the same `key` always resolves to the same
    calendar event, so upserts are idempotent."""
    digest = hashlib.sha256(key.encode()).digest()
    return base64.b32hexencode(digest).decode().lower().rstrip("=")[:40]


# --- action ledger CRUD --------------------------------------------------------

def plan_action(db_path: str, action: Action) -> Action:
    """Insert a new action, or (if `idempotency_key` already exists) update it
    in place with the new payload — Section 11.1's "same key = same row" rule."""
    conn = db.get_connection(db_path)
    existing = get_action_by_key(db_path, action.idempotency_key)
    now = _now_iso()

    if existing is None:
        conn.execute(
            "INSERT INTO actions (action_id, idempotency_key, drive_id, drive_version, app, kind, tier, "
            "payload, status, attempts, last_error, lease_until, result, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, ?, ?)",
            (
                action.action_id, action.idempotency_key, action.drive_id, action.drive_version,
                action.app, action.kind, action.tier, json.dumps(action.payload, default=str),
                action.status, now, now,
            ),
        )
        conn.commit()
        return action

    if existing.payload == action.payload and existing.status not in ("FAILED", "VOIDED"):
        return existing  # unchanged: no-op

    conn.execute(
        "UPDATE actions SET drive_version = ?, payload = ?, status = 'PLANNED', attempts = 0, "
        "last_error = NULL, lease_until = NULL, updated_at = ? WHERE idempotency_key = ?",
        (action.drive_version, json.dumps(action.payload, default=str), now, action.idempotency_key),
    )
    conn.commit()
    return get_action_by_key(db_path, action.idempotency_key)  # type: ignore[return-value]


def _row_to_action(row) -> Action:
    return Action(
        action_id=row["action_id"], idempotency_key=row["idempotency_key"], drive_id=row["drive_id"],
        drive_version=row["drive_version"], app=row["app"], kind=row["kind"], tier=row["tier"],
        payload=json.loads(row["payload"]), status=row["status"], attempts=row["attempts"],
        last_error=row["last_error"],
        lease_until=datetime.fromisoformat(row["lease_until"]) if row["lease_until"] else None,
        result=json.loads(row["result"]) if row["result"] else None,
    )


def get_action_by_key(db_path: str, idempotency_key: str) -> Action | None:
    conn = db.get_connection(db_path)
    row = conn.execute("SELECT * FROM actions WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
    return _row_to_action(row) if row else None


def get_actions_for_drive(db_path: str, drive_id: str) -> list[Action]:
    conn = db.get_connection(db_path)
    rows = conn.execute("SELECT * FROM actions WHERE drive_id = ? ORDER BY created_at ASC", (drive_id,)).fetchall()
    return [_row_to_action(r) for r in rows]


def get_pending_actions(db_path: str) -> list[Action]:
    conn = db.get_connection(db_path)
    now = _now_iso()
    # ORDER BY rowid, not created_at: two actions planned in the same burst
    # can tie on the ISO timestamp (sub-millisecond clock resolution), and
    # action_id (a random UUID) is not a safe tiebreaker — it made execution
    # order, and therefore which action a chaos "crash" lands on, flaky.
    # SQLite's implicit rowid reflects true insertion order and is untouched
    # by later UPDATEs (re-planning), so FIFO order is preserved exactly.
    # `<=`, not `<`: a lease set with lease_seconds=0 (chaos/test recovery)
    # can get the *same* ISO timestamp string on both the set and the check —
    # two datetime.now() calls a few bytecodes apart can land in the same
    # clock tick. A lease expiring at exactly `now` should count as expired,
    # not "still valid for one more instant"; `<` missed that boundary and
    # made crash-recovery flaky (an action could sit stuck in EXECUTING).
    rows = conn.execute(
        "SELECT * FROM actions WHERE status IN ('PLANNED','APPROVED') "
        "OR (status = 'EXECUTING' AND lease_until <= ?) "
        "ORDER BY rowid ASC",
        (now,),
    ).fetchall()
    return [_row_to_action(r) for r in rows]


def _save_action(db_path: str, action: Action) -> None:
    conn = db.get_connection(db_path)
    conn.execute(
        "UPDATE actions SET status = ?, attempts = ?, last_error = ?, lease_until = ?, result = ?, "
        "updated_at = ? WHERE action_id = ?",
        (
            action.status, action.attempts, action.last_error,
            action.lease_until.isoformat() if action.lease_until else None,
            json.dumps(action.result, default=str) if action.result is not None else None,
            _now_iso(), action.action_id,
        ),
    )
    conn.commit()


def void_action_by_key(db_path: str, idempotency_key: str) -> None:
    """Void an action that hasn't executed yet (still PLANNED/AWAITING_APPROVAL/APPROVED)."""
    action = get_action_by_key(db_path, idempotency_key)
    if action is not None and action.status in ("PLANNED", "AWAITING_APPROVAL", "APPROVED"):
        action.status = "VOIDED"
        _save_action(db_path, action)


# --- approvals -----------------------------------------------------------------

def new_short_token() -> str:
    """A short opaque token for Telegram `callback_data` (Section 6.5): full
    drive_ids are slugs like "zentrix-analytics:data-analyst:2026" and won't
    reliably fit — or stay collision-free when truncated — inside the 64-byte
    callback_data limit alongside other tokens."""
    return uuid.uuid4().hex[:6]


def create_approval(db_path: str, action_id: str, drive_id: str, drive_version: int,
                     approval_id: str | None = None) -> str:
    conn = db.get_connection(db_path)
    approval_id = approval_id or new_short_token()
    conn.execute(
        "INSERT INTO approvals (approval_id, action_id, drive_id, drive_version, telegram_message_id, "
        "status, decided_at) VALUES (?, ?, ?, ?, NULL, 'PENDING', NULL)",
        (approval_id, action_id, drive_id, drive_version),
    )
    conn.commit()
    return approval_id


def void_pending_approvals(db_path: str, drive_id: str) -> None:
    conn = db.get_connection(db_path)
    conn.execute(
        "UPDATE approvals SET status = 'VOIDED' WHERE drive_id = ? AND status = 'PENDING'",
        (drive_id,),
    )
    conn.commit()


def get_pending_approval(db_path: str, drive_id: str) -> dict | None:
    conn = db.get_connection(db_path)
    row = conn.execute(
        "SELECT * FROM approvals WHERE drive_id = ? AND status = 'PENDING' ORDER BY approval_id DESC LIMIT 1",
        (drive_id,),
    ).fetchone()
    return dict(row) if row else None


SNOOZE_SECONDS = 2 * 60 * 60  # "Remind me in 2h"


def snooze_approval(db_path: str, approval_id: str, now: datetime) -> None:
    """"Remind me in 2h": takes the approval out of PENDING (so it stops
    showing as an active question) without voiding it, and records when to
    resurface it. Found live: this used to just say "OK" and do nothing —
    the deadline reminder on the calendar was the only thing that still
    fired, with no actual follow-up message ever sent."""
    conn = db.get_connection(db_path)
    conn.execute(
        "UPDATE approvals SET status = 'SNOOZED', snoozed_until = ? WHERE approval_id = ?",
        ((now + timedelta(seconds=SNOOZE_SECONDS)).isoformat(), approval_id),
    )
    conn.commit()


def get_due_snoozed_approvals(db_path: str, now: datetime) -> list[dict]:
    conn = db.get_connection(db_path)
    rows = conn.execute(
        "SELECT * FROM approvals WHERE status = 'SNOOZED' AND snoozed_until <= ?", (now.isoformat(),)
    ).fetchall()
    return [dict(r) for r in rows]


# --- retry / circuit breaker -----------------------------------------------------

def backoff_seconds(attempt: int) -> float:
    return min(0.5 * (2 ** attempt), 8.0) + random.uniform(0, 0.25)


@dataclass
class CircuitState:
    consecutive_failures: int = 0
    opened_until: datetime | None = None

    def is_open(self, now: datetime) -> bool:
        return self.opened_until is not None and now < self.opened_until

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_until = None

    def record_failure(self, now: datetime) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= CIRCUIT_FAILURE_THRESHOLD:
            self.opened_until = now + timedelta(seconds=CIRCUIT_OPEN_SECONDS)


class CircuitBreaker:
    def __init__(self) -> None:
        self._states: dict[str, CircuitState] = {}

    def state_for(self, app: str) -> CircuitState:
        return self._states.setdefault(app, CircuitState())

    def snapshot(self, now: datetime | None = None) -> dict[str, dict]:
        """Read-only view for the dashboard's health panel — never mutates
        state, so it's safe to call from a different thread than the one
        actually running actions. `now` is injectable so a test's "open"
        assertion never depends on how much real wall-clock time has passed
        since it recorded a failure — found live: a test hardcoding a `now`
        for record_failure() but relying on this method's real `datetime.
        now()` passed for the first few hours of this session and then
        started failing once real time moved past that hardcoded window,
        entirely on its own, with no code change at all."""
        now = now or datetime.now(timezone.utc)
        return {
            app: {
                "consecutive_failures": state.consecutive_failures,
                "open": state.is_open(now),
                "opened_until": state.opened_until.isoformat() if state.opened_until else None,
            }
            for app, state in self._states.items()
        }


# Found live: run_pending's own `breaker or CircuitBreaker()` default made
# main.py's worker loop implicitly create a brand-new, empty breaker every
# single poll (nothing was ever passed in) — so a circuit only ever
# protected the batch of actions within one 15-second poll, never
# remembered a run of failures across polls the way a real circuit breaker
# is supposed to. One shared instance per db_path (mirroring db.py's own
# per-path connection cache) gives it real memory, and lets the dashboard's
# health panel and the worker loop see the exact same live state.
_shared_breakers: dict[str, CircuitBreaker] = {}


def shared_breaker(db_path: str) -> CircuitBreaker:
    return _shared_breakers.setdefault(db_path, CircuitBreaker())


def with_retry(fn, *, on_attempt=None, sleep=time.sleep) -> tuple[bool, object | None, str | None]:
    """Runs `fn()` with retry/backoff. Returns (ok, result, error_message)."""
    attempt = 0
    while True:
        try:
            result = fn()
            return True, result, None
        except AdapterError as e:
            attempt += 1
            if on_attempt:
                on_attempt(attempt, e)
            retryable = e.status_code in RETRYABLE_STATUS_CODES or e.status_code is None
            if not retryable or attempt >= MAX_ATTEMPTS:
                return False, None, str(e)
            wait = e.retry_after if e.retry_after is not None else backoff_seconds(attempt)
            sleep(wait)


@dataclass
class Adapters:
    sheets: SheetStore
    calendar: CalendarStore
    messenger: Messenger
    # Optional: only cutoff.bot.telegram_loop's resume-choice handler needs
    # this (Section 6.4 extension), to run the static resume match when the
    # student answers "use my resume on file." Every other Adapters
    # construction site (including every test written before this feature
    # existed) doesn't pass it and is unaffected.
    files: FileStore | None = None


def _execute_one(action: Action, adapters: Adapters) -> dict:
    if action.app == "sheets" and action.kind == "upsert_row":
        adapters.sheets.upsert_drive_row(action.drive_id, action.payload["row"])
        return {}

    if action.app == "calendar" and action.kind == "upsert_event":
        event_id = event_id_for(action.idempotency_key)
        event = CalendarEvent.model_validate(action.payload["event"])
        adapters.calendar.upsert_event(event_id, event)
        return {"event_id": event_id}

    if action.app == "calendar" and action.kind == "cancel_event":
        adapters.calendar.cancel_event(action.payload["event_id"])
        return {"event_id": action.payload["event_id"]}

    if action.app == "telegram" and action.kind == "send_msg":
        buttons = [Button.model_validate(b) for b in action.payload.get("buttons") or []] or None
        prior_message_id = (action.result or {}).get("message_id")
        if prior_message_id:
            adapters.messenger.edit(prior_message_id, action.payload["text"], buttons)
            return {"message_id": prior_message_id}
        message_id = adapters.messenger.send(action.idempotency_key, action.payload["text"], buttons)
        return {"message_id": message_id}

    raise ValueError(f"no executor for app={action.app} kind={action.kind}")


def run_pending(
    db_path: str, adapters: Adapters, breaker: CircuitBreaker | None = None, *,
    lease_seconds: int = LEASE_SECONDS, raise_after_actions: int | None = None,
) -> list[Action]:
    """Executes every PLANNED/APPROVED action once (Section 11.4), plus any
    EXECUTING action whose lease has expired (Section 11.6: crash-resume).
    Returns the actions touched, in the order processed.

    `raise_after_actions` is chaos-only (Section 15.4 `crash_mid_run`): the
    (N+1)th action is marked EXECUTING (leased) and then `SimulatedCrash` is
    raised before it actually runs — exactly the leased-but-untouched state a
    real process kill mid-write would leave behind. The first N actions
    complete normally."""
    trace.configure(db_path)
    breaker = breaker or CircuitBreaker()
    processed: list[Action] = []
    started = 0

    for action in get_pending_actions(db_path):
        circuit = breaker.state_for(action.app)
        now = datetime.now(timezone.utc)
        if circuit.is_open(now):
            continue  # "degraded, retrying" — left PLANNED for the next pass

        started += 1
        action.status = "EXECUTING"
        action.lease_until = now + timedelta(seconds=lease_seconds)
        _save_action(db_path, action)

        if raise_after_actions is not None and started > raise_after_actions:
            raise SimulatedCrash(f"simulated crash while executing action {started}")

        with trace.span(f"execute:{action.kind}", app=action.app, drive_id=action.drive_id):
            ok, result, error = with_retry(lambda: _execute_one(action, adapters))

        if ok:
            circuit.record_success()
            action.status = "DONE"
            action.result = result if isinstance(result, dict) else (action.result or {})
            action.attempts += 1
            action.last_error = None
            _save_action(db_path, action)
            action = _verify_and_update(db_path, adapters, action)
        else:
            circuit.record_failure(now)
            action.status = "FAILED"
            action.attempts += 1
            action.last_error = error
            _save_action(db_path, action)

        processed.append(action)

    return processed


def _verify_with_retry(adapters: Adapters, action: Action) -> bool:
    """verifier.verify() re-reads real app state to confirm a write actually
    landed — the one mechanism meant to catch a silent failure. Found live:
    this call had zero retry protection of its own, so a transient failure
    on the *confirmation read* (not even the original write) crashed the
    whole poll cycle uncaught (calendar_flaky chaos profile: 45.2% recovery
    vs 90%+ for every other profile). Wrapping it in with_retry means a
    flaky read gets retried like every other adapter call; if retries are
    genuinely exhausted, that's treated as "not verified" — which already
    triggers the existing re-execute-and-reverify fallback below — never an
    uncaught crash."""
    from cutoff.pipeline import verifier  # local import: verifier depends on executor's Adapters type

    ok_call, verified, _error = with_retry(lambda: verifier.verify(adapters, action))
    return bool(verified) if ok_call else False


def _verify_and_update(db_path: str, adapters: Adapters, action: Action) -> Action:
    ok = _verify_with_retry(adapters, action)
    if not ok:
        with trace.span(f"execute:{action.kind}:reverify", app=action.app, drive_id=action.drive_id):
            retry_ok, result, error = with_retry(lambda: _execute_one(action, adapters))
        if retry_ok:
            action.result = result if isinstance(result, dict) else action.result
            ok = _verify_with_retry(adapters, action)

    action.status = "VERIFIED" if ok else "FAILED"
    if not ok and action.last_error is None:
        action.last_error = "verification failed after re-execution"
    _save_action(db_path, action)
    return action

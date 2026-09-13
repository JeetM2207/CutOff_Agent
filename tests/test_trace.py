"""trace.latest_run_id (Section 16 dashboard support). Regression test for a
live UX finding: the literal most-recent run is very often a trivial
one-span run (a duplicate email delivery, or a "nothing changed"
short-circuit) — on a demo board this made the Live Trace tab look stuck on
a single "ingest" span far more often than not. Prefers the most recent run
with more than one span; falls back to the literal latest when nothing else
qualifies."""
from cutoff import db, trace


def _fresh_db(tmp_path):
    path = str(tmp_path / "cutoff.db")
    db.init_db(path)
    trace.configure(path)
    return path


def test_prefers_most_recent_multi_span_run_over_a_later_trivial_one(tmp_path):
    _fresh_db(tmp_path)

    with trace.run("run-full"):
        with trace.span("ingest", message_id="m1"):
            pass
        with trace.span("security", from_addr="x"):
            pass

    with trace.run("run-duplicate"):
        with trace.span("ingest", message_id="m2"):
            pass  # short-circuits here — only one span, like a duplicate/no-op message

    assert trace.latest_run_id() == "run-full"


def test_falls_back_to_literal_latest_when_nothing_has_more_than_one_span(tmp_path):
    _fresh_db(tmp_path)

    with trace.run("run-a"):
        with trace.span("ingest", message_id="m1"):
            pass

    with trace.run("run-b"):
        with trace.span("ingest", message_id="m2"):
            pass

    assert trace.latest_run_id() == "run-b"


def test_returns_none_when_no_runs_exist(tmp_path):
    _fresh_db(tmp_path)
    assert trace.latest_run_id() is None

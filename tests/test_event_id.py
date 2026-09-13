"""Section 6.3: deterministic, base32hex, Calendar-API-compatible event IDs."""
import re

from cutoff.pipeline.executor import event_id_for

BASE32HEX_RE = re.compile(r"^[a-v0-9]{5,40}$")


def test_deterministic_for_same_key():
    assert event_id_for("cal:d1:deadline") == event_id_for("cal:d1:deadline")


def test_different_for_different_keys():
    assert event_id_for("cal:d1:deadline") != event_id_for("cal:d2:deadline")


def test_output_is_valid_base32hex_and_within_length_bounds():
    event_id = event_id_for("cal:zentrix-analytics:data-analyst:2026:deadline")
    assert BASE32HEX_RE.match(event_id)
    assert 5 <= len(event_id) <= 40

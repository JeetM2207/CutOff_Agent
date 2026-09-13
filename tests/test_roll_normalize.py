"""Section 13: roll-number normalization for shortlist matching."""
from cutoff.pipeline.shortlist import normalize_roll_no


def test_strips_dashes_slashes_dots_and_spaces():
    assert normalize_roll_no("21-BCS-045") == "21BCS045"
    assert normalize_roll_no("21/BCS/045") == "21BCS045"
    assert normalize_roll_no("21.BCS.045") == "21BCS045"
    assert normalize_roll_no("21 BCS 045") == "21BCS045"


def test_uppercases():
    assert normalize_roll_no("21bcs045") == "21BCS045"


def test_different_formats_of_the_same_roll_number_match():
    assert normalize_roll_no("21-BCS-045") == normalize_roll_no("21bcs045") == normalize_roll_no("21 BCS 045")

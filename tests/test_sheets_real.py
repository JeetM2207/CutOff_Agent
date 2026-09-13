"""SheetsSource.upsert_drive_row / read_drive_row (Section 6.2) against a
mocked googleapiclient service — no real network. Regression test for a live
bug: a None field (e.g. no deadline stated) got str()'d into the literal
text "None" on write, then read back as that same string — never equal to
the real Python None the planner compares against in verifier.verify, so
every no-deadline drive's sheets row failed verification forever.

Also regression-tests a second live bug: a real HttpError from Sheets used
to propagate raw out of every method here, which with_retry() (only catches
AdapterError) never saw — so a real 429/500 got none of the retry/backoff/
circuit-breaker treatment the eval harness's simulated chaos gets."""
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError

from cutoff.adapters.sheets_real import DRIVES_HEADER, SheetsSource
from cutoff.pipeline.executor import AdapterError


class _FakeExecute:
    def __init__(self, result=None, error=None):
        self._result = result or {}
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return self._result


class _FakeValues:
    """In-memory single-row-at-a-time store, just enough of the
    spreadsheets().values() surface SheetsSource actually calls."""

    def __init__(self):
        self.rows: dict[str, list[str]] = {}  # drive_id -> row values
        self._order: list[str] = []
        self.form_templates: list[list[str]] = []  # [[form_url, template_url], ...]
        self.get_error: Exception | None = None

    def get(self, spreadsheetId, range):
        if self.get_error is not None:
            return _FakeExecute(error=self.get_error)
        if range == "Drives!A:A":
            return _FakeExecute({"values": [[did] for did in self._order]})
        if range == "FormTemplates!A:B":
            return _FakeExecute({"values": self.form_templates})
        # "Drives!A{row_index}:K{row_index}"
        row_index = int(range.split("A", 1)[1].split(":")[0])
        drive_id = self._order[row_index - 1]
        return _FakeExecute({"values": [self.rows[drive_id]]})

    def append(self, spreadsheetId, range, valueInputOption, insertDataOption, body):
        values = body["values"][0]
        drive_id = values[0]
        self.rows[drive_id] = values
        self._order.append(drive_id)
        return _FakeExecute()

    def update(self, spreadsheetId, range, valueInputOption, body):
        row_index = int(range.split("A", 1)[1].split(":")[0])
        drive_id = self._order[row_index - 1]
        self.rows[drive_id] = body["values"][0]
        return _FakeExecute()


class _FakeSpreadsheets:
    def __init__(self, values: _FakeValues):
        self._values = values

    def values(self):
        return self._values


class _FakeService:
    def __init__(self):
        self.values = _FakeValues()

    def spreadsheets(self):
        return _FakeSpreadsheets(self.values)


def test_upsert_then_read_roundtrips_none_deadline_as_none():
    service = _FakeService()
    src = SheetsSource(service, "sheet1")

    src.upsert_drive_row("drive1", {
        "company": "Test Systems", "role": "SWE", "version": 1,
        "verdict": "ELIGIBLE", "reasons": "", "deadline": None, "status": "OPEN",
    })

    raw = service.values.rows["drive1"]
    deadline_col = DRIVES_HEADER.index("deadline")
    assert raw[deadline_col] == ""  # not the literal string "None"

    row = src.read_drive_row("drive1")
    assert row["deadline"] is None
    assert row["verdict"] == "ELIGIBLE"


def test_upsert_then_read_roundtrips_real_deadline_unchanged():
    service = _FakeService()
    src = SheetsSource(service, "sheet1")

    src.upsert_drive_row("drive1", {
        "company": "Test Systems", "role": "SWE", "version": 1,
        "verdict": "ELIGIBLE", "reasons": "", "deadline": "2026-09-20T23:59:00+05:30", "status": "OPEN",
    })

    row = src.read_drive_row("drive1")
    assert row["deadline"] == "2026-09-20T23:59:00+05:30"


def test_read_missing_row_returns_none():
    service = _FakeService()
    src = SheetsSource(service, "sheet1")
    assert src.read_drive_row("nope") is None


def test_read_form_template_finds_matching_row():
    service = _FakeService()
    service.values.form_templates = [
        ["https://forms.gle/abc", "https://docs.google.com/forms/d/e/X/viewform?entry.1=student_name"],
        ["https://forms.gle/xyz", "https://docs.google.com/forms/d/e/Y/viewform?entry.2=student_roll_no"],
    ]
    src = SheetsSource(service, "sheet1")

    assert src.read_form_template("https://forms.gle/xyz") == (
        "https://docs.google.com/forms/d/e/Y/viewform?entry.2=student_roll_no"
    )


def test_read_form_template_returns_none_when_no_match():
    service = _FakeService()
    service.values.form_templates = [["https://forms.gle/abc", "https://docs.google.com/forms/d/e/X/viewform"]]
    src = SheetsSource(service, "sheet1")

    assert src.read_form_template("https://forms.gle/not-there") is None


def test_read_drive_row_translates_http_error_to_adapter_error():
    service = _FakeService()
    service.values.get_error = HttpError(resp=SimpleNamespace(status=500, reason="Internal Error"), content=b"{}")
    src = SheetsSource(service, "sheet1")

    with pytest.raises(AdapterError) as exc_info:
        src.read_drive_row("drive1")
    assert exc_info.value.status_code == 500

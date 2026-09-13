"""forms_scrape.py: parsing a Google Form's embedded FB_PUBLIC_LOAD_DATA_
structure (no network — parse_form_fields is a pure function over an HTML
string), and fetch_form_page's redirect/error handling against a monkey-
patched httpx.get (Section 0 rule 3: no real network calls in tests)."""
import json

import httpx
import pytest

from cutoff.adapters import forms_scrape
from cutoff.adapters.forms_scrape import FILLABLE_TYPES, fetch_form_page, parse_form_fields

# Matches the real (unofficial, community-documented) structure:
# data[1][1] = list of questions; per question: [_, title, _, type, [[entry_id, options, required]]]
_FB_DATA = [
    None,
    [None, [
        [100, "Full Name", None, 0, [[111111111, None, 1]]],
        [101, "Why should we hire you?", None, 1, [[222222222, None, 0]]],
        [102, "Years of experience", None, 2, [[333333333, [["0-1"], ["1-3"]], 1]]],
    ]],
]


def _html(data=_FB_DATA) -> str:
    return f"<html><script>var FB_PUBLIC_LOAD_DATA_ = {json.dumps(data)};</script></html>"


def test_parse_form_fields_extracts_all_questions():
    fields = parse_form_fields(_html())
    assert len(fields) == 3
    by_title = {f.title: f for f in fields}
    assert by_title["Full Name"].entry_id == "111111111"
    assert by_title["Full Name"].field_type == 0
    assert by_title["Full Name"].required is True
    assert by_title["Why should we hire you?"].field_type == 1
    assert by_title["Why should we hire you?"].required is False
    assert by_title["Years of experience"].field_type == 2


def test_fillable_types_excludes_choice_questions():
    fields = [f for f in parse_form_fields(_html()) if f.field_type in FILLABLE_TYPES]
    titles = {f.title for f in fields}
    assert titles == {"Full Name", "Why should we hire you?"}
    assert "Years of experience" not in titles


def test_parse_form_fields_returns_empty_list_when_marker_missing():
    assert parse_form_fields("<html><body>not a form</body></html>") == []


def test_parse_form_fields_returns_empty_list_on_malformed_json():
    html = "<script>var FB_PUBLIC_LOAD_DATA_ = [1, 2, not valid json];</script>"
    assert parse_form_fields(html) == []


def test_parse_form_fields_handles_brackets_inside_question_title():
    data = [None, [None, [
        [100, "Rate your skill [1-5]", None, 0, [[555, None, 1]]],
    ]]]
    fields = parse_form_fields(_html(data))
    assert len(fields) == 1
    assert fields[0].title == "Rate your skill [1-5]"


def test_fetch_form_page_returns_resolved_url_and_html(monkeypatch):
    class _FakeResponse:
        url = "https://docs.google.com/forms/d/e/abc123/viewform"
        text = "<html>form page</html>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(forms_scrape.httpx, "get", lambda *a, **k: _FakeResponse())

    result = fetch_form_page("https://forms.gle/abc123")
    assert result == ("https://docs.google.com/forms/d/e/abc123/viewform", "<html>form page</html>")


def test_fetch_form_page_returns_none_on_network_error(monkeypatch):
    def _raise(*a, **k):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(forms_scrape.httpx, "get", _raise)
    assert fetch_form_page("https://forms.gle/abc123") is None


def test_fetch_form_page_returns_none_when_not_a_google_form(monkeypatch):
    class _FakeResponse:
        url = "https://example.com/careers/apply"
        text = "<html>a company's own ATS page</html>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(forms_scrape.httpx, "get", lambda *a, **k: _FakeResponse())
    assert fetch_form_page("https://example.com/careers/apply") is None

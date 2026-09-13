"""form_prefill.build_prefilled_url (Section 6.5 extension): substitutes the
student's real profile values for placeholder tokens captured in a stored
Google Forms "Get pre-filled link" template. Never calls any network or API
— pure URL string manipulation."""
from urllib.parse import parse_qs, urlparse

from cutoff.models import StudentProfile
from cutoff.pipeline import form_prefill

PROFILE = StudentProfile(
    name="Riya Mehta", roll_no="21BCS045", email="riya@college.edu", branch="CSE",
    gpa=7.42, active_backlogs=0, batch_year=2026,
)


def test_returns_none_when_no_template():
    assert form_prefill.build_prefilled_url(None, PROFILE) is None
    assert form_prefill.build_prefilled_url("", PROFILE) is None


def test_substitutes_matched_placeholder_tokens():
    template = (
        "https://docs.google.com/forms/d/e/1FAIpQ/viewform?usp=pp_url"
        "&entry.111=student_name&entry.222=student_roll_no&entry.333=student_branch"
    )
    url = form_prefill.build_prefilled_url(template, PROFILE)
    assert url is not None
    params = parse_qs(urlparse(url).query)
    assert params["entry.111"] == ["Riya Mehta"]
    assert params["entry.222"] == ["21BCS045"]
    assert params["entry.333"] == ["CSE"]
    assert params["usp"] == ["pp_url"]


def test_placeholder_matching_is_case_insensitive():
    template = "https://docs.google.com/forms/d/e/X/viewform?entry.1=STUDENT_NAME"
    url = form_prefill.build_prefilled_url(template, PROFILE)
    params = parse_qs(urlparse(url).query)
    assert params["entry.1"] == ["Riya Mehta"]


def test_leaves_non_placeholder_values_untouched():
    template = "https://docs.google.com/forms/d/e/X/viewform?entry.1=student_name&entry.2=some+other+default"
    url = form_prefill.build_prefilled_url(template, PROFILE)
    params = parse_qs(urlparse(url).query)
    assert params["entry.1"] == ["Riya Mehta"]
    assert params["entry.2"] == ["some other default"]


def test_returns_none_when_nothing_matches():
    template = "https://docs.google.com/forms/d/e/X/viewform?entry.1=some+random+default"
    assert form_prefill.build_prefilled_url(template, PROFILE) is None


def test_returns_none_on_template_with_no_query_string():
    assert form_prefill.build_prefilled_url("https://docs.google.com/forms/d/e/X/viewform", PROFILE) is None

"""cutoff/pipeline/security.py (Section 12): sender allowlist, lookalike/
spoof detection, and content scam signals. Found live (a deep-research pass
after "why is the eval always 100%?"): this security-critical module had
zero dedicated unit tests at all — only indirect coverage through eval
scenarios that exercise the full pipeline, never these functions in
isolation. Every function gets direct coverage here, plus the homoglyph
detection fix and its own regression test."""
from cutoff.models import CollegePolicy
from cutoff.pipeline import security


def policy(**overrides) -> CollegePolicy:
    base = dict(
        career_office_senders=["careers.demo.college@gmail.com"],
        college_domain="college.edu", allowed_form_domains=["docs.google.com", "forms.gle"],
    )
    base.update(overrides)
    return CollegePolicy(**base)


# --- extract_email / extract_display_name ------------------------------------

def test_extract_email_from_display_name_format():
    assert security.extract_email("Career Office <careers@college.edu>") == "careers@college.edu"


def test_extract_email_from_bare_address():
    assert security.extract_email("careers@college.edu") == "careers@college.edu"


def test_extract_display_name_present():
    assert security.extract_display_name('"Career Office" <careers@college.edu>') == "Career Office"


def test_extract_display_name_absent_is_empty():
    assert security.extract_display_name("careers@college.edu") == ""


# --- is_allowlisted -------------------------------------------------------------

def test_allowlisted_by_exact_sender():
    assert security.is_allowlisted("Career Office <careers.demo.college@gmail.com>", policy())


def test_allowlisted_by_college_domain():
    assert security.is_allowlisted("Someone <dean@college.edu>", policy())


def test_not_allowlisted_unrelated_sender():
    assert not security.is_allowlisted("Scammer <scam@evil.com>", policy())


def test_sender_matching_is_case_insensitive():
    assert security.is_allowlisted("CAREERS.DEMO.COLLEGE@GMAIL.COM", policy())


# --- check_lookalike -------------------------------------------------------------

def test_exact_domain_is_not_a_lookalike():
    assert not security.check_lookalike("Real <careers@college.edu>", policy())


def test_unrelated_domain_is_not_a_lookalike():
    """A totally unrelated domain isn't "close" to the real one — it's just
    not allowlisted, a different (and correct) code path entirely."""
    assert not security.check_lookalike("Random <a@totallyunrelated.com>", policy())


def test_single_character_swap_is_a_lookalike():
    assert security.check_lookalike("Fake <careers@co1lege.edu>", policy())


def test_domain_two_edits_away_is_a_lookalike():
    assert security.check_lookalike("Fake <careers@colledge.edu>", policy())


def test_domain_three_edits_away_is_not_flagged_by_edit_distance_alone():
    """Documents the actual boundary of the edit-distance check on its own
    (FUZZY_REVIEW_BAND-style tolerance is deliberately narrow) — a domain
    this different is expected to be treated as unrelated, not lookalike."""
    assert not security.check_lookalike("Fake <careers@colleged123.edu>", policy())


def test_homoglyph_domain_is_flagged_even_with_three_substitutions():
    """Found live: a domain with 3 Cyrillic look-alike characters (с/о/е for
    c/o/e) reads as visually identical to "college.edu" to a human, but its
    Levenshtein distance (3) exceeds the plain edit-distance threshold (2) —
    so the check missed it before homoglyph normalization was added."""
    spoofed_domain = "college.edu".translate(str.maketrans("coe", "сое"))
    assert security.check_lookalike(f"Career Office <fake@{spoofed_domain}>", policy())


def test_homoglyph_translation_never_fires_on_the_real_domain():
    assert not security.check_lookalike("Real <careers@college.edu>", policy())


# --- check_display_spoof --------------------------------------------------------

def test_display_spoof_flags_career_office_wording_on_unrelated_address():
    assert security.check_display_spoof('"Career Office" <urgent@fastpay-verify.example>', policy())


def test_display_spoof_flags_placement_wording_on_unrelated_address():
    assert security.check_display_spoof('"Placement Cell" <notice@random-mail.com>', policy())


def test_display_spoof_never_flags_the_real_allowlisted_sender():
    """Defense in depth check (Section 12): the trigger words are common in
    the real career office's own legitimate mail, so display-spoof must
    never fire for an address that's already allowlisted."""
    assert not security.check_display_spoof('"Career Office" <careers.demo.college@gmail.com>', policy())


def test_display_spoof_ignores_unrelated_display_names():
    assert not security.check_display_spoof('"John Doe" <john@evil.com>', policy())


# --- scan_fee_language / scan_content --------------------------------------------

def test_scan_fee_language_catches_registration_fee():
    assert security.scan_fee_language("Please pay the registration fee of Rs 999.")


def test_scan_fee_language_catches_upi_mention():
    assert security.scan_fee_language("Pay via UPI to confirm@fastpay.example")


def test_scan_fee_language_catches_rupee_amount():
    assert security.scan_fee_language("A fee of ₹500 is required.")


def test_scan_fee_language_ignores_ordinary_drive_email():
    text = "Eligibility: CSE, IT | CGPA 7.0 and above | No active backlogs."
    assert not security.scan_fee_language(text)


def test_scan_content_returns_fee_request_signal():
    assert security.scan_content("guaranteed selection, pay now to confirm") == ["FEE_REQUEST"]


def test_scan_content_empty_for_clean_text():
    assert security.scan_content("Eligibility: CSE, IT | CGPA 7.0 and above.") == []


# --- check_sender (the combined entry point) -------------------------------------

def test_check_sender_allowed_with_no_signals():
    allowed, signals = security.check_sender("Career Office <careers.demo.college@gmail.com>", policy())
    assert allowed
    assert signals == []


def test_check_sender_flags_lookalike_and_is_not_allowed():
    allowed, signals = security.check_sender("Fake <careers@co1lege.edu>", policy())
    assert not allowed
    assert "LOOKALIKE_SENDER" in signals


def test_check_sender_flags_display_spoof_and_is_not_allowed():
    allowed, signals = security.check_sender('"Career Office" <urgent@fastpay-verify.example>', policy())
    assert not allowed
    assert "DISPLAY_NAME_SPOOF" in signals


def test_check_sender_unrelated_sender_is_not_allowed_with_no_signals():
    """An unrelated, non-deceptive sender is correctly just "not allowed" —
    no signals, since it isn't impersonating anything."""
    allowed, signals = security.check_sender("Random <a@totallyunrelated.com>", policy())
    assert not allowed
    assert signals == []


# --- check_form_domain -----------------------------------------------------------

def test_form_domain_allowed_for_forms_gle():
    assert security.check_form_domain("https://forms.gle/abc123", policy())


def test_form_domain_allowed_for_docs_google_subdomain():
    assert security.check_form_domain("https://docs.google.com/forms/d/e/xyz/viewform", policy())


def test_form_domain_allowed_for_college_domain_itself():
    assert security.check_form_domain("https://portal.college.edu/register", policy())


def test_form_domain_rejects_off_domain_link():
    assert not security.check_form_domain("https://fastpay-verify.example/pay", policy())


def test_form_domain_none_url_is_treated_as_allowed():
    """No form URL stated at all isn't itself suspicious — only an actual
    off-domain link is."""
    assert security.check_form_domain(None, policy())

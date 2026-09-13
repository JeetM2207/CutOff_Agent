"""Sender allowlist, lookalike/spoof detection, and content scam signals
(Section 12). Architecture is the defense: a non-allowlisted sender never
reaches the LLM extractor at all — these checks are deterministic and local."""
from __future__ import annotations

import re

from rapidfuzz.distance import Levenshtein

from cutoff.models import CollegePolicy

DISPLAY_NAME_TRIGGERS = ("placement", "career office", "t&p", "tnp")

# A practical, dependency-free subset of Unicode confusables — the Cyrillic
# and Greek letters real lookalike-domain phishing actually uses, each
# visually near-identical to a Latin letter. Not the full Unicode
# confusables table (would need an external package), but covers the
# realistic threat model. Found live: a domain with 3+ of these substituted
# (e.g. Cyrillic с/о/е for c/o/e) is visually indistinguishable from the
# real one but has a Levenshtein distance > 2, so the plain edit-distance
# check alone let it through.
_HOMOGLYPH_TRANSLATION = str.maketrans({
    "а": "a", "А": "A",  # Cyrillic a / Greek alpha share this shape
    "е": "e", "Е": "E",  # Cyrillic ie
    "о": "o", "О": "O",  # Cyrillic o / Greek omicron
    "р": "p", "Р": "P",  # Cyrillic er / Greek rho
    "с": "c", "С": "C",  # Cyrillic es
    "у": "y", "У": "Y",  # Cyrillic u / Greek upsilon
    "х": "x", "Х": "X",  # Cyrillic ha / Greek chi
    "і": "i", "І": "I",  # Cyrillic/Ukrainian i
    "ј": "j", "Ј": "J",  # Cyrillic je
    "ѕ": "s", "Ѕ": "S",  # Cyrillic dze
    "ԁ": "d",            # Cyrillic komi de
    "ν": "v",            # Greek nu
    "α": "a", "Α": "A",  # Greek alpha (lowercase)
    "ο": "o", "Ο": "O",  # Greek omicron (lowercase)
    "υ": "u", "Υ": "Y",  # Greek upsilon (lowercase)
})


def _deconfuse(text: str) -> str:
    """Maps known look-alike characters back to plain ASCII, so a domain
    that's visually identical to the real one reads as identical here too."""
    return text.translate(_HOMOGLYPH_TRANSLATION)

_FEE_PATTERNS = [
    re.compile(r"registration fee", re.IGNORECASE),
    re.compile(r"refundable deposit", re.IGNORECASE),
    re.compile(r"guarante(e|ed)\s+selection", re.IGNORECASE),
    re.compile(r"\bUPI\b"),
    re.compile(r"₹\s*\d"),  # ₹<digits>
    re.compile(r"confirm your seat", re.IGNORECASE),
    re.compile(r"\bpay\b.{0,20}\b(now|today|to confirm)\b", re.IGNORECASE),
]


def extract_email(from_addr: str) -> str:
    if "<" in from_addr and ">" in from_addr:
        return from_addr.split("<", 1)[1].split(">", 1)[0].strip()
    return from_addr.strip()


def extract_display_name(from_addr: str) -> str:
    if "<" in from_addr:
        return from_addr.split("<", 1)[0].strip().strip('"')
    return ""


def is_allowlisted(from_addr: str, policy: CollegePolicy) -> bool:
    email = extract_email(from_addr).lower()
    if email in {s.lower() for s in policy.career_office_senders}:
        return True
    domain = email.split("@")[-1] if "@" in email else ""
    return bool(domain) and domain == policy.college_domain.lower()


def check_lookalike(from_addr: str, policy: CollegePolicy) -> bool:
    email = extract_email(from_addr).lower()
    domain = email.split("@")[-1] if "@" in email else ""
    college_domain = policy.college_domain.lower()
    if not domain or domain == college_domain:
        return False
    # Homoglyphs first: a domain that reads as identical to the real one
    # once look-alike characters are mapped back to ASCII is a lookalike
    # regardless of how many characters were substituted — found live,
    # plain edit-distance alone missed a 3-character-substituted domain
    # that's visually indistinguishable from the real one.
    if _deconfuse(domain) == college_domain:
        return True
    dist = Levenshtein.distance(domain, college_domain)
    return 0 < dist <= 2


def check_display_spoof(from_addr: str, policy: CollegePolicy) -> bool:
    if is_allowlisted(from_addr, policy):
        return False
    name = extract_display_name(from_addr).lower()
    return any(trigger in name for trigger in DISPLAY_NAME_TRIGGERS)


def scan_fee_language(text: str) -> bool:
    return any(p.search(text) for p in _FEE_PATTERNS)


def check_sender(from_addr: str, policy: CollegePolicy) -> tuple[bool, list[str]]:
    """Returns (allowed, sender_signals). Signals are populated even for
    allowed senders (defense in depth), but only matter when not allowed."""
    signals: list[str] = []
    if check_lookalike(from_addr, policy):
        signals.append("LOOKALIKE_SENDER")
    if check_display_spoof(from_addr, policy):
        signals.append("DISPLAY_NAME_SPOOF")
    return is_allowlisted(from_addr, policy), signals


def scan_content(text: str) -> list[str]:
    signals = []
    if scan_fee_language(text):
        signals.append("FEE_REQUEST")
    return signals


def check_form_domain(form_url: str | None, policy: CollegePolicy) -> bool:
    """True if the form URL's domain is allowed."""
    if not form_url:
        return True
    m = re.search(r"https?://([^/]+)", form_url)
    if not m:
        return False
    host = m.group(1).lower()
    allowed = {d.lower() for d in policy.allowed_form_domains} | {policy.college_domain.lower()}
    return any(host == d or host.endswith("." + d) for d in allowed)

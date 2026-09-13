"""Shortlist PDF matching (Section 13): page-by-page roll-number search with
evidence, never trusting a name-only match."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

MIN_READABLE_PAGE_CHARS = 20
_STRIP_CHARS = re.compile(r"[\s\-/.]+")


def normalize_roll_no(roll_no: str) -> str:
    return _STRIP_CHARS.sub("", roll_no).upper()


@dataclass
class ShortlistResult:
    status: str  # "SHORTLISTED" | "NAME_ONLY_MATCH" | "NOT_LISTED" | "UNREADABLE"
    page_number: int | None = None
    evidence_line: str | None = None
    pages_extracted: int = 0
    events: list[dict] = field(default_factory=list)


def extract_pages(pdf_bytes: bytes) -> list[str]:
    import pdfplumber

    pages = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return pages


def match(pdf_bytes: bytes, roll_no: str, full_name: str) -> ShortlistResult:
    pages = extract_pages(pdf_bytes)

    if not pages or all(len(p.strip()) < MIN_READABLE_PAGE_CHARS for p in pages):
        return ShortlistResult(status="UNREADABLE", pages_extracted=len(pages))

    target_roll = normalize_roll_no(roll_no)
    if target_roll:
        for i, page_text in enumerate(pages, start=1):
            for line in page_text.splitlines():
                if target_roll in normalize_roll_no(line):
                    return ShortlistResult(
                        status="SHORTLISTED", page_number=i, evidence_line=line.strip(), pages_extracted=len(pages)
                    )

    name_lower = full_name.strip().lower()
    if name_lower:
        for i, page_text in enumerate(pages, start=1):
            for line in page_text.splitlines():
                if name_lower in line.lower():
                    return ShortlistResult(
                        status="NAME_ONLY_MATCH", page_number=i, evidence_line=line.strip(), pages_extracted=len(pages)
                    )

    return ShortlistResult(status="NOT_LISTED", pages_extracted=len(pages))

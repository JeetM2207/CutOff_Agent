"""Section 13: shortlist PDF matching, against real PDFs (reportlab), read
with real pdfplumber — no fakes, since this is pure local file processing."""
import io

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from cutoff.pipeline.shortlist import match


def _make_pdf(pages: list[list[str]]) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _, height = A4
    for lines in pages:
        y = height - 50
        for line in lines:
            c.drawString(50, y, line)
            y -= 18
        c.showPage()
    c.save()
    return buf.getvalue()


def test_exact_roll_match_reports_shortlisted_with_page_evidence():
    pdf = _make_pdf([["21BCS012 Aman Verma", "21BCS045 Riya Mehta", "21BCS078 Kabir Singh"]])
    result = match(pdf, "21BCS045", "Riya Mehta")
    assert result.status == "SHORTLISTED"
    assert result.page_number == 1
    assert "21BCS045" in result.evidence_line


def test_differently_formatted_roll_still_matches():
    pdf = _make_pdf([["21-BCS-045   Riya Mehta"]])
    result = match(pdf, "21BCS045", "Riya Mehta")
    assert result.status == "SHORTLISTED"


def test_roll_match_wins_on_a_later_page():
    pdf = _make_pdf([["21BCS012 Aman Verma", "Page one of the shortlist"],
                      ["21BCS045 Riya Mehta", "Page two of the shortlist"]])
    result = match(pdf, "21BCS045", "Riya Mehta")
    assert result.status == "SHORTLISTED"
    assert result.page_number == 2


def test_same_name_different_roll_is_never_reported_shortlisted():
    pdf = _make_pdf([["Shortlist for Round 2", "21BCS054 Riya Mehta"]])  # different roll, same name
    result = match(pdf, "21BCS045", "Riya Mehta")
    assert result.status == "NAME_ONLY_MATCH"
    assert result.page_number == 1


def test_no_match_at_all():
    pdf = _make_pdf([["21BCS012 Aman Verma", "21BCS078 Kabir Singh"]])
    result = match(pdf, "21BCS045", "Riya Mehta")
    assert result.status == "NOT_LISTED"


def test_scanned_looking_pdf_is_unreadable():
    pdf = _make_pdf([[]])  # a page with no extractable text at all
    result = match(pdf, "21BCS045", "Riya Mehta")
    assert result.status == "UNREADABLE"


def test_roll_match_takes_priority_over_name_match_on_a_different_row():
    pdf = _make_pdf([["21BCS045 Someone Else", "21BCS099 Riya Mehta"]])
    result = match(pdf, "21BCS045", "Riya Mehta")
    assert result.status == "SHORTLISTED"
    assert "21BCS045" in result.evidence_line

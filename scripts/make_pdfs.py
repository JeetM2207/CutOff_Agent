"""Generates shortlist PDFs for eval fixtures (Section 17), using reportlab.
Run directly to (re)generate the shortlist attachments referenced by
eval/fixtures/dev/dev_020..022's scenario.json files:

    python scripts/make_pdfs.py
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "eval" / "fixtures" / "dev"


def make_shortlist_pdf(path: str | Path, pages: list[list[str]], title: str) -> None:
    """`pages` is a list of pages, each a list of lines to render."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    for page_lines in pages:
        y = height - 50
        c.setFont("Helvetica-Bold", 14)
        c.drawString(50, y, title)
        y -= 30
        c.setFont("Helvetica", 11)
        for line in page_lines:
            c.drawString(50, y, line)
            y -= 18
        c.showPage()
    c.save()


def main() -> None:
    # dev_020: roll number present in a different format ("21-BCS-045")
    make_shortlist_pdf(
        FIXTURES_DIR / "dev_020_shortlist_roll_different_format" / "attachments" / "shortlist.pdf",
        pages=[[
            "Roll No          Name                Branch",
            "21BCS012         Aman Verma          CSE",
            "21-BCS-045       Riya Mehta          CSE",
            "21BCS078         Kabir Singh         CSE",
        ]],
        title="Zentrix Analytics - Shortlist for Online Test (Round 1)",
    )

    # dev_021: same name, different roll number — must NOT report "shortlisted"
    make_shortlist_pdf(
        FIXTURES_DIR / "dev_021_shortlist_name_only_match" / "attachments" / "shortlist.pdf",
        pages=[
            ["Roll No          Name                Branch"],
            [
                "21BCS019         Kabir Singh         IT",
                "21BCS054         Riya Mehta          ECE",
                "21BCS091         Aman Verma          CSE",
            ],
        ],
        title="Northwind Systems - Shortlist for Technical Interview",
    )

    # dev_022: clean, exact roll-number match
    make_shortlist_pdf(
        FIXTURES_DIR / "dev_022_shortlist_included_clean" / "attachments" / "shortlist.pdf",
        pages=[[
            "Roll No          Name                Branch",
            "21BCS003         Neha Kulkarni       CSE",
            "21BCS045         Riya Mehta          CSE",
            "21BCS066         Sana Iyer           IT",
        ]],
        title="Cobalt Freight - Operations Analyst Shortlist",
    )
    print(f"Wrote shortlist PDFs under {FIXTURES_DIR}")


if __name__ == "__main__":
    main()

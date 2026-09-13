"""cutoff.pipeline.ingest.extract_pdf_hyperlinks: recovering the real URL
behind a PDF's clickable link text (e.g. a resume's `\\href{url}{GitHub}`),
which extract_pdf_text alone can never see — pdfplumber's plain-text
extraction discards hyperlink annotations entirely. Real reportlab-built
PDFs, real pdfplumber extraction; nothing mocked, nothing needs to be."""
import io

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from cutoff.pipeline.ingest import extract_pdf_hyperlinks, extract_pdf_text


def _pdf_with_links(links: list[tuple[str, str]]) -> bytes:
    """`links` is [(visible_text, target_url), ...], one per line."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _, height = A4
    y = height - 50
    c.setFont("Helvetica", 12)
    for text, url in links:
        c.drawString(50, y, text)
        c.linkURL(url, (50, y - 2, 50 + len(text) * 7, y + 10), relative=0)
        y -= 20
    c.showPage()
    c.save()
    return buf.getvalue()


def test_extract_pdf_text_only_returns_the_visible_link_label_never_the_url():
    """The bug this whole module exists to work around: plain-text
    extraction alone cannot recover a hyperlink's real target."""
    pdf_bytes = _pdf_with_links([("GitHub", "https://github.com/JeetM2207")])
    text = extract_pdf_text(pdf_bytes)
    assert "GitHub" in text
    assert "github.com" not in text


def test_extract_pdf_hyperlinks_recovers_the_real_url():
    pdf_bytes = _pdf_with_links([("GitHub", "https://github.com/JeetM2207")])
    assert extract_pdf_hyperlinks(pdf_bytes) == ["https://github.com/JeetM2207"]


def test_extract_pdf_hyperlinks_returns_every_link_in_order():
    pdf_bytes = _pdf_with_links([
        ("LinkedIn", "https://linkedin.com/in/jeet-manseta"),
        ("GitHub", "https://github.com/JeetM2207"),
        ("Live Demo", "https://legalsahai.vercel.app/login"),
    ])
    assert extract_pdf_hyperlinks(pdf_bytes) == [
        "https://linkedin.com/in/jeet-manseta", "https://github.com/JeetM2207", "https://legalsahai.vercel.app/login",
    ]


def test_extract_pdf_hyperlinks_dedupes_repeated_urls():
    pdf_bytes = _pdf_with_links([
        ("GitHub", "https://github.com/JeetM2207"),
        ("GitHub (again)", "https://github.com/JeetM2207"),
    ])
    assert extract_pdf_hyperlinks(pdf_bytes) == ["https://github.com/JeetM2207"]


def test_extract_pdf_hyperlinks_returns_empty_list_for_a_pdf_with_no_links():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.drawString(50, 750, "No links here.")
    c.showPage()
    c.save()
    assert extract_pdf_hyperlinks(buf.getvalue()) == []

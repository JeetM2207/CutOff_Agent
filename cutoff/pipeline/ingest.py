"""Ingest (Section 8, step 1): normalize the email body, split off quoted reply
chains, and pull text out of any PDF attachment."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from cutoff.adapters.base import MailSource
from cutoff.models import EmailMessage
from cutoff.pipeline.executor import with_retry

_TAG_RE = re.compile(r"<[^>]+>")

_QUOTE_MARKERS = [
    re.compile(r"\n\s*On .{0,120}? wrote:\s*\n", re.IGNORECASE),
    re.compile(r"\n-{2,}\s*Original Message\s*-{2,}\n", re.IGNORECASE),
    re.compile(r"\nFrom:.+\nSent:.+\nTo:.+\nSubject:.+\n", re.IGNORECASE),
]


def strip_html(text: str) -> str:
    if "<" not in text or ">" not in text:
        return text
    without_tags = _TAG_RE.sub(" ", text)
    return " ".join(without_tags.split())


def split_quoted(body_text: str) -> tuple[str, str]:
    """Returns (clean_body, quoted_history)."""
    earliest: int | None = None
    for pattern in _QUOTE_MARKERS:
        m = pattern.search(body_text)
        if m and (earliest is None or m.start() < earliest):
            earliest = m.start()
    if earliest is None:
        return body_text.strip(), ""
    return body_text[:earliest].strip(), body_text[earliest:].strip()


@dataclass
class IngestedEmail:
    body_text: str
    quoted_history: str
    attachment_text: str
    full_text: str = field(init=False)

    def __post_init__(self) -> None:
        self.full_text = "\n".join(
            part for part in (self.body_text, self.quoted_history, self.attachment_text) if part
        )


def extract_pdf_text(data: bytes) -> str:
    import pdfplumber

    pages_text = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            pages_text.append(page.extract_text() or "")
    return "\n".join(pages_text)


def extract_pdf_hyperlinks(data: bytes) -> list[str]:
    """Returns every unique clickable-link URI actually embedded in the PDF
    (e.g. a real \\href target in a LaTeX-built resume) — found live
    (cutoff.llm.profile_extract's onboarding parser): `extract_pdf_text`
    above only returns a link's VISIBLE text ("GitHub", "Live Demo"), never
    the URL it points to — pdfplumber's plain-text extraction discards
    hyperlink annotations entirely. Without this, an LLM reading only the
    plain text has no way to recover the real URL behind a label like
    that, and can only guess (which is exactly what it did: echoing the
    label back as if it were the URL)."""
    import pdfplumber

    urls: list[str] = []
    seen: set[str] = set()
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            for link in page.hyperlinks:
                uri = link.get("uri")
                if uri and uri not in seen:
                    seen.add(uri)
                    urls.append(uri)
    return urls


def ingest(msg: EmailMessage, mail: MailSource) -> IngestedEmail:
    clean, quoted = split_quoted(strip_html(msg.body_text))

    attachment_texts = []
    for att in msg.attachments:
        if att.mime_type == "application/pdf" or att.filename.lower().endswith(".pdf"):
            # Reads aren't ledgered (Section 11.2) but still need the same
            # retry/backoff as writes — a transient 429/500 here otherwise
            # kills the whole message with zero resilience.
            ok, data, err = with_retry(lambda att=att: mail.get_attachment(msg.message_id, att.attachment_id))
            if not ok:
                raise RuntimeError(f"couldn't fetch attachment {att.filename!r}: {err}")
            attachment_texts.append(extract_pdf_text(data))

    return IngestedEmail(body_text=clean, quoted_history=quoted, attachment_text="\n".join(attachment_texts))

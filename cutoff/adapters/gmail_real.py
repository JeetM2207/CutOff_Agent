"""Real Gmail adapter (Section 6.1). gmail.readonly only — the agent can
never send, delete, or modify mail."""
from __future__ import annotations

import base64
from datetime import datetime, timezone

from googleapiclient.errors import HttpError

from cutoff.adapters.google_auth import raise_as_adapter_error
from cutoff.models import AttachmentMeta, EmailMessage, EmailRef
from cutoff.pipeline.ingest import strip_html


class GmailSource:
    def __init__(self, service):
        self._service = service

    def list_new(self, senders: list[str], since: datetime) -> list[EmailRef]:
        # -in:drafts: found live — an auto-saved draft (composed to one of
        # the allow-listed senders, e.g. a student emailing themselves a
        # test drive) matches `from:` just like a real sent message, but
        # its content is whatever was typed so far — incomplete text, no
        # attachment yet — which the LLM can reasonably read as suspicious.
        # The draft later either gets sent (a new, complete message with
        # its own id) or discarded; either way it was never real mail.
        base = f"({' OR '.join(f'from:{s}' for s in senders)})" if senders else ""
        query = f"{base} newer_than:30d -in:drafts".strip()
        return self._search(query, since)

    def list_suspicious(self, keywords: list[str], since: datetime) -> list[EmailRef]:
        # No `from:` restriction at all — this is what actually lets a
        # lookalike-domain or spoofed-display-name sender reach
        # security.py's check_lookalike/check_display_spoof, which existed
        # all along but could never fire in real polling before this,
        # since list_new above is deliberately narrowed to the allowlist.
        # Keyword-scoped, not "everyone", so this stays safe against a
        # real, high-volume inbox (an unscoped query starves the poll loop
        # — see Section 6.1's own note on why that was never turned on).
        if not keywords:
            return []
        base = "(" + " OR ".join(f'"{k}"' for k in keywords) + ")"
        query = f"{base} newer_than:30d -in:drafts"
        return self._search(query, since)

    def _search(self, query: str, since: datetime) -> list[EmailRef]:
        refs: list[EmailRef] = []
        page_token = None
        try:
            while True:
                resp = self._service.users().messages().list(
                    userId="me", q=query, pageToken=page_token
                ).execute()
                for m in resp.get("messages", []):
                    meta = self._service.users().messages().get(
                        userId="me", id=m["id"], format="metadata", metadataHeaders=["From", "Subject"]
                    ).execute()
                    received_at = datetime.fromtimestamp(int(meta["internalDate"]) / 1000, tz=timezone.utc)
                    if received_at < since:
                        continue
                    headers = {h["name"]: h["value"] for h in meta["payload"].get("headers", [])}
                    refs.append(EmailRef(
                        message_id=meta["id"], thread_id=meta["threadId"],
                        from_addr=headers.get("From", ""), subject=headers.get("Subject", ""),
                        received_at=received_at,
                    ))
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        except HttpError as e:
            raise_as_adapter_error(e)
        return refs

    def get(self, message_id: str) -> EmailMessage:
        try:
            full = self._service.users().messages().get(userId="me", id=message_id, format="full").execute()
        except HttpError as e:
            raise_as_adapter_error(e)
        headers = {h["name"]: h["value"] for h in full["payload"].get("headers", [])}
        received_at = datetime.fromtimestamp(int(full["internalDate"]) / 1000, tz=timezone.utc)
        body_text, attachments = _extract_body_and_attachments(full["payload"])
        return EmailMessage(
            message_id=full["id"], thread_id=full["threadId"],
            from_addr=headers.get("From", ""), subject=headers.get("Subject", ""),
            body_text=body_text, received_at=received_at, attachments=attachments,
        )

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        try:
            att = self._service.users().messages().attachments().get(
                userId="me", messageId=message_id, id=attachment_id
            ).execute()
        except HttpError as e:
            raise_as_adapter_error(e)
        return base64.urlsafe_b64decode(att["data"])


def _extract_body_and_attachments(payload: dict) -> tuple[str, list[AttachmentMeta]]:
    plain_text = ""
    html_text = ""
    attachments: list[AttachmentMeta] = []

    stack = [payload]
    while stack:
        part = stack.pop()
        mime = part.get("mimeType", "")
        filename = part.get("filename") or ""
        body = part.get("body", {})

        if filename and body.get("attachmentId"):
            attachments.append(AttachmentMeta(
                attachment_id=body["attachmentId"], filename=filename,
                mime_type=mime, size_bytes=body.get("size", 0),
            ))
        elif mime == "text/plain" and body.get("data"):
            plain_text += base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="replace")
        elif mime == "text/html" and body.get("data"):
            html_text += base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="replace")

        stack.extend(part.get("parts", []) or [])

    body_text = plain_text or strip_html(html_text)
    return body_text, attachments

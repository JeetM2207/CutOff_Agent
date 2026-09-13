"""One-time OAuth flow -> token.json (Section 20), and authorized
googleapiclient service builders with an optional base_url override for
Arga twins (Section 18)."""
from __future__ import annotations

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from cutoff.pipeline.executor import AdapterError

# Least privilege (Section 12): gmail.readonly and drive.readonly only —
# the agent can never send/delete mail or write to Drive.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive.readonly",
]


def load_credentials(client_secret_file: str, token_file: str) -> Credentials:
    """Loads a cached token if valid, refreshes it if expired, or runs the
    interactive browser consent flow (Section 20) and caches the result."""
    creds: Credentials | None = None
    token_path = Path(token_file)
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return creds


def build_service(name: str, version: str, creds: Credentials, base_url: str | None = None):
    kwargs = {}
    if base_url:
        kwargs["client_options"] = {"api_endpoint": base_url}
    return build(name, version, credentials=creds, cache_discovery=False, **kwargs)


def raise_as_adapter_error(e: HttpError) -> None:
    """Translates a real googleapiclient HttpError into the AdapterError
    vocabulary with_retry() actually understands, so a transient Gmail/
    Sheets/Drive 429 or 5xx gets the same retry/backoff/circuit-breaker
    treatment the eval harness's simulated chaos already gets — and so a
    permanent error (403/404, e.g. a resume that's a native Google Doc, not
    a downloadable file) fails fast with one clean message instead of an
    unhandled exception killing the whole run. Found live: telegram_real.py
    already did this translation; gmail_real.py/sheets_real.py/drive_real.py
    never did, so with_retry's `except AdapterError` never caught a single
    real Google API error — only Telegram's and the eval harness's fakes."""
    status = e.resp.status if e.resp is not None else None
    retry_after = None
    get = getattr(e.resp, "get", None)
    if callable(get):
        raw = get("retry-after")
        if raw:
            try:
                retry_after = float(raw)
            except (TypeError, ValueError):
                pass
    raise AdapterError(str(e), status_code=status, retry_after=retry_after) from e

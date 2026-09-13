"""One-time OAuth flow (Section 20). Opens a browser for you to approve
access, then saves token.json. Re-run if the token expires (Testing-mode
refresh tokens can expire after about a week):

    python scripts/google_auth.py
"""
from __future__ import annotations

from cutoff.adapters.google_auth import load_credentials
from cutoff.config import get_settings


def main() -> None:
    settings = get_settings()
    load_credentials(settings.google_client_secret_file, settings.google_token_file)
    print(f"OK: wrote {settings.google_token_file}")


if __name__ == "__main__":
    main()

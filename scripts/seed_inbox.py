"""Sends fixture emails from the "career office" account (Section 17), so you
can watch a real email flow through the running agent. Uses a SEPARATE OAuth
token with gmail.send scope — the main agent's own credentials are
gmail.readonly only (Section 12) and must never be able to send mail.

First run prompts you to sign in as the CAREER OFFICE account (not the
student account CutOff itself reads from) and saves sender_token.json.

    python scripts/seed_inbox.py --to riya@example.com --subject "..." --body "..."
    python scripts/seed_inbox.py --to riya@example.com --preset new_drive
"""
from __future__ import annotations

import argparse
import base64
import io
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from cutoff.config import get_settings

SEND_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
SENDER_TOKEN_FILE = "sender_token.json"


def _shortlist_pdf_bytes() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _, height = A4
    y = height - 50
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, y, "Meridian Robotics - Software Engineer Shortlist")
    y -= 30
    c.setFont("Helvetica", 11)
    for line in [
        "Roll No          Name                Branch",
        "21BCS012         Aman Verma          CSE",
        "21BCS045         Riya Mehta          CSE",
        "21BCS078         Kabir Singh         CSE",
    ]:
        c.drawString(50, y, line)
        y -= 18
    c.showPage()
    c.save()
    return buf.getvalue()


# Matches CUTOFF_SPEC.md Section 20's demo checklist: 3 new drives, 1
# correction, 1 shortlist PDF, 1 scam — six emails ready to send live.
PRESETS = {
    "new_drive": {
        "subject": "Campus Drive: Meridian Robotics - Software Engineer (2026 Batch)",
        "body": (
            "Dear Students,\n\nMeridian Robotics is visiting campus for the Software Engineer role "
            "(CTC 11 LPA).\nEligibility: B.Tech CSE, IT, ECE | CGPA 7.0 and above | No active backlogs.\n"
            "Register here: https://forms.gle/demoMeridian by 11:59 PM, tomorrow.\n\nRegards,\nCareer Office"
        ),
    },
    "new_drive_2": {
        # Deliberately a NEEDS_REVIEW case for demo variety: a generic
        # aggregate percentage with no 10th/12th mention always needs a
        # human, since the eligibility engine has no field for it to check.
        "subject": "Campus Drive: Vantage Retail - Business Analyst (2026 Batch)",
        "body": (
            "Dear Students,\n\nVantage Retail is hiring Business Analysts (CTC 9 LPA).\n"
            "Eligibility: CSE, IT | Minimum 60% aggregate throughout academics | No active backlogs.\n"
            "Register here: https://forms.gle/demoVantage by 11:59 PM, tomorrow.\n\nRegards,\nCareer Office"
        ),
    },
    "new_drive_3": {
        # Deliberately a NOT_ELIGIBLE case for demo variety (branch-restricted).
        "subject": "Campus Drive: Kestrel Robotics - Firmware Engineer (2026 Batch)",
        "body": (
            "Dear Students,\n\nKestrel Robotics is hiring Firmware Engineers (CTC 14 LPA).\n"
            "Eligibility: ECE, EEE only | CGPA 7.0 and above | No active backlogs.\n"
            "Register here: https://forms.gle/demoKestrel by 11:59 PM, tomorrow.\n\nRegards,\nCareer Office"
        ),
    },
    "correction": {
        "subject": "Re: Campus Drive: Meridian Robotics - Software Engineer (2026 Batch)",
        "body": "Correction: ECE students are no longer eligible for this drive. Eligible branches: CSE, IT only.",
    },
    "scam": {
        "subject": "CONGRATULATIONS! Pay registration fee to confirm your seat",
        "body": (
            "You have been PRE-SELECTED. Pay a refundable registration fee of Rs 999 via UPI to "
            "confirm@fastpay-verify.example within 1 hour or lose this opportunity. Selection guaranteed."
        ),
    },
    "shortlist": {
        "subject": "Shortlist: Meridian Robotics - Software Engineer",
        "body": (
            "Dear Students,\n\nPlease find attached the shortlist for the Meridian Robotics Software "
            "Engineer drive.\n\nRegards,\nCareer Office"
        ),
        "attachment": ("shortlist.pdf", _shortlist_pdf_bytes),
    },
}


def _load_sender_credentials(client_secret_file: str) -> Credentials:
    creds = None
    token_path = Path(SENDER_TOKEN_FILE)
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(SENDER_TOKEN_FILE, SEND_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            print("Sign in as the CAREER OFFICE account (not the student account CutOff reads from).")
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, SEND_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
    return creds


def send(to: str, subject: str, body: str, client_secret_file: str, attachment: tuple[str, bytes] | None = None) -> None:
    creds = _load_sender_credentials(client_secret_file)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    if attachment:
        filename, data = attachment
        message = MIMEMultipart()
        message.attach(MIMEText(body))
        part = MIMEApplication(data, _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=filename)
        message.attach(part)
    else:
        message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"Sent: {subject!r} -> {to}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", required=True, help="student inbox CutOff reads from")
    parser.add_argument("--preset", choices=sorted(PRESETS))
    parser.add_argument("--subject")
    parser.add_argument("--body")
    args = parser.parse_args()

    attachment = None
    if args.preset:
        preset = PRESETS[args.preset]
        subject, body = preset["subject"], preset["body"]
        if "attachment" in preset:
            filename, make_bytes = preset["attachment"]
            attachment = (filename, make_bytes())
    elif args.subject and args.body:
        subject, body = args.subject, args.body
    else:
        raise SystemExit("Pass either --preset <name> or both --subject and --body")

    settings = get_settings()
    send(args.to, subject, body, settings.google_client_secret_file, attachment)


if __name__ == "__main__":
    main()

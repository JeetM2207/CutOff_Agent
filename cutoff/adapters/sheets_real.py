"""Real Google Sheets adapter (Section 6.2). Tabs: Profile, Policy, Drives, Log.
Upsert reads column A, finds the row by drive_id, and updates it — never
appends blindly."""
from __future__ import annotations

from googleapiclient.errors import HttpError

from cutoff.adapters.google_auth import raise_as_adapter_error
from cutoff.models import CollegePolicy, StudentProfile

DRIVES_HEADER = [
    "drive_id", "company", "role", "version", "verdict", "reasons",
    "deadline", "status", "resume", "last_updated", "evidence_link",
]
DRIVES_LAST_COL = "K"  # len(DRIVES_HEADER) columns, A..K


class SheetsSource:
    def __init__(self, service, sheet_id: str):
        self._service = service
        self._sheet_id = sheet_id

    def _values(self):
        return self._service.spreadsheets().values()

    def _execute(self, request):
        try:
            return request.execute()
        except HttpError as e:
            raise_as_adapter_error(e)

    def _read_kv(self, tab: str) -> dict:
        resp = self._execute(self._values().get(spreadsheetId=self._sheet_id, range=f"{tab}!A:B"))
        return {row[0]: row[1] for row in resp.get("values", []) if len(row) >= 2}

    def read_profile(self) -> StudentProfile:
        kv = self._read_kv("Profile")
        return StudentProfile(
            name=kv["name"], roll_no=kv["roll_no"], email=kv["email"], branch=kv["branch"],
            gpa=float(kv["gpa"]), gpa_scale=float(kv.get("gpa_scale", 10.0)),
            active_backlogs=int(kv["active_backlogs"]),
            pct_10th=float(kv["pct_10th"]) if kv.get("pct_10th") else None,
            pct_12th=float(kv["pct_12th"]) if kv.get("pct_12th") else None,
            batch_year=int(kv["batch_year"]),
            placed_status=kv.get("placed_status", "UNPLACED"),
            current_offer_lpa=float(kv["current_offer_lpa"]) if kv.get("current_offer_lpa") else None,
            timezone=kv.get("timezone", "Asia/Kolkata"),
        )

    def read_policy(self) -> CollegePolicy:
        kv = self._read_kv("Policy")
        return CollegePolicy(
            career_office_senders=[s.strip() for s in kv.get("career_office_senders", "").split(",") if s.strip()],
            college_domain=kv["college_domain"],
            one_offer_rule=kv.get("one_offer_rule", "").strip().lower() in ("true", "1", "yes"),
            dream_multiplier=float(kv.get("dream_multiplier", 1.0)),
            no_show_penalty_text=kv.get("no_show_penalty_text", ""),
            allowed_form_domains=[
                s.strip() for s in kv.get("allowed_form_domains", "docs.google.com,forms.gle").split(",") if s.strip()
            ],
        )

    def _find_drive_row_index(self, drive_id: str) -> int | None:
        resp = self._execute(self._values().get(spreadsheetId=self._sheet_id, range="Drives!A:A"))
        for i, row in enumerate(resp.get("values", []), start=1):
            if row and row[0] == drive_id:
                return i
        return None

    def upsert_drive_row(self, drive_id: str, row: dict) -> None:
        existing = self.read_drive_row(drive_id) or {}
        merged = {**existing, **row, "drive_id": drive_id}
        # `merged.get(col, "")`'s default only fires for a missing key — a
        # key present with value None (e.g. no deadline stated) still hits
        # str(), writing the literal text "None" into the cell. Found live:
        # that made verify() permanently fail for every no-deadline drive,
        # since read_drive_row read "None" back as a string while the
        # planned row's `deadline` was the real Python None.
        values = ["" if merged.get(col) is None else str(merged.get(col, "")) for col in DRIVES_HEADER]

        row_index = self._find_drive_row_index(drive_id)
        if row_index is None:
            self._execute(self._values().append(
                spreadsheetId=self._sheet_id, range=f"Drives!A:{DRIVES_LAST_COL}",
                valueInputOption="RAW", insertDataOption="INSERT_ROWS", body={"values": [values]},
            ))
        else:
            self._execute(self._values().update(
                spreadsheetId=self._sheet_id, range=f"Drives!A{row_index}:{DRIVES_LAST_COL}{row_index}",
                valueInputOption="RAW", body={"values": [values]},
            ))

    def read_drive_row(self, drive_id: str) -> dict | None:
        row_index = self._find_drive_row_index(drive_id)
        if row_index is None:
            return None
        resp = self._execute(self._values().get(
            spreadsheetId=self._sheet_id, range=f"Drives!A{row_index}:{DRIVES_LAST_COL}{row_index}"
        ))
        values = resp.get("values", [[]])[0] if resp.get("values") else []
        row = {col: (values[i] if i < len(values) else "") for i, col in enumerate(DRIVES_HEADER)}
        if row.get("version"):
            try:
                row["version"] = int(row["version"])
            except ValueError:
                pass
        # Mirror upsert_drive_row's None -> "" on write: `deadline` and
        # `verdict` are the two Drives columns _sheets_row() can plan as
        # real None (no deadline stated / no verdict yet), so an empty cell
        # for either must read back as None, not "", to match what verify()
        # compares against.
        for col in ("deadline", "verdict"):
            if row.get(col) == "":
                row[col] = None
        return row

    def read_form_template(self, form_url: str) -> str | None:
        """FormTemplates tab (A: form_url, B: template_url) — one row per
        distinct form layout, captured once via Google's own "Get pre-filled
        link" (Section 6.5 extension). No header-row assumption beyond
        column order, same as the Drives tab."""
        resp = self._execute(self._values().get(spreadsheetId=self._sheet_id, range="FormTemplates!A:B"))
        for row in resp.get("values", []):
            if row and row[0] == form_url:
                return row[1] if len(row) >= 2 else None
        return None

    def append_log(self, drive_id: str, action: str, result: str, timestamp: str) -> None:
        self._execute(self._values().append(
            spreadsheetId=self._sheet_id, range="Log!A:D",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [[timestamp, drive_id, action, result]]},
        ))

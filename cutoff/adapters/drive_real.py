"""Real Google Drive adapter (Section 6.4). drive.readonly only."""
from __future__ import annotations

from googleapiclient.errors import HttpError

from cutoff.adapters.google_auth import raise_as_adapter_error
from cutoff.models import ResumeFile


class DriveSource:
    def __init__(self, service, resume_folder_id: str):
        self._service = service
        self._folder_id = resume_folder_id

    def list_resumes(self) -> list[ResumeFile]:
        # Found live: a resume folder can itself contain a subfolder (e.g.
        # "Updated_resume") — with no mimeType filter, that subfolder came
        # back as if it were a resume file, and later crashed
        # get_resume_content (folders have no downloadable content).
        try:
            resp = self._service.files().list(
                q=(f"'{self._folder_id}' in parents and trashed = false "
                   "and mimeType != 'application/vnd.google-apps.folder'"),
                fields="files(id,name,webViewLink)",
            ).execute()
        except HttpError as e:
            raise_as_adapter_error(e)
        return [
            ResumeFile(file_id=f["id"], name=f["name"], web_view_link=f.get("webViewLink", ""))
            for f in resp.get("files", [])
        ]

    def get_resume_content(self, file_id: str) -> bytes:
        try:
            return self._service.files().get_media(fileId=file_id).execute()
        except HttpError as e:
            raise_as_adapter_error(e)

"""DriveSource.list_resumes / get_resume_content (Section 6.4) against a
mocked googleapiclient service — no real network. Regression test for a
live bug: a resume folder containing a subfolder (e.g. "Updated_resume")
had that subfolder returned as if it were a resume file — later crashing
get_resume_content, since folders have no downloadable content. Also
regression-tests a second live bug: a real HttpError (e.g. the 403
"fileNotDownloadable" that subfolder crash actually produced) used to
propagate raw, which with_retry() (only catches AdapterError) never saw —
so resume.select_resume_smart's own "skip this resume, don't block
planning" contract silently didn't hold for real Drive errors."""
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError

from cutoff.adapters.drive_real import DriveSource
from cutoff.pipeline.executor import AdapterError


class _FakeExecute:
    def __init__(self, result=None, error=None):
        self._result = result or {}
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return self._result


class _FakeFiles:
    def __init__(self):
        self.list_queries: list[str] = []
        self.list_result: dict = {"files": []}
        self.get_media_error: Exception | None = None

    def list(self, q, fields):
        self.list_queries.append(q)
        return _FakeExecute(self.list_result)

    def get_media(self, fileId):
        if self.get_media_error is not None:
            return _FakeExecute(error=self.get_media_error)
        return _FakeExecute(b"pdf-bytes")


class _FakeService:
    def __init__(self):
        self.files_ = _FakeFiles()

    def files(self):
        return self.files_


def test_list_resumes_query_excludes_folders():
    service = _FakeService()
    src = DriveSource(service, "folder1")

    src.list_resumes()

    query = service.files_.list_queries[0]
    assert "'folder1' in parents" in query
    assert "mimeType != 'application/vnd.google-apps.folder'" in query


def test_list_resumes_returns_only_files_the_query_matched():
    service = _FakeService()
    service.files_.list_result = {"files": [
        {"id": "f1", "name": "resume_SDE.pdf", "webViewLink": "https://drive/f1"},
    ]}
    src = DriveSource(service, "folder1")

    resumes = src.list_resumes()

    assert len(resumes) == 1
    assert resumes[0].name == "resume_SDE.pdf"


def test_get_resume_content_translates_http_error_to_adapter_error():
    service = _FakeService()
    # The real error a subfolder's id produces when fetched as if it were a file.
    service.files_.get_media_error = HttpError(
        resp=SimpleNamespace(status=403, reason="Forbidden"), content=b"{}"
    )
    src = DriveSource(service, "folder1")

    with pytest.raises(AdapterError) as exc_info:
        src.get_resume_content("some-subfolder-id")
    assert exc_info.value.status_code == 403

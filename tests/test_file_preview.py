"""A stored project file can be inspected without starting a run."""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import workspace
from app.core.files import save_upload
from app.web.file_preview import build_file_preview
from app.web.loading import PREVIEW_ROWS_SHOWN

client = TestClient(app)


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "demo").mkdir(parents=True)
    (tmp_path / "other").mkdir(parents=True)
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setenv("CARBON_PAPER_FILES_ROOT", str(tmp_path / "files"))


def store(name: str, body: bytes, project_id: str = "demo") -> str:
    return save_upload(name, io.BytesIO(body), project_id).id


def preview_url(file_id: str, project_id: str = "demo") -> str:
    return f"/project/{project_id}/files/{file_id}/preview"


def test_preview_reads_the_file_into_a_typed_bounded_view(project):
    rows = "\n".join(f"row-{index}" for index in range(PREVIEW_ROWS_SHOWN + 1))
    file_id = store("stories.csv", f"name\n{rows}\n".encode())

    preview = build_file_preview("demo", file_id)

    assert preview.filename == "stories.csv"
    assert preview.format == "csv"
    assert preview.columns == ["name"]
    assert preview.row_count == PREVIEW_ROWS_SHOWN + 1
    assert len(preview.rows) == PREVIEW_ROWS_SHOWN
    assert preview.rows[-1] == [f"row-{PREVIEW_ROWS_SHOWN - 1}"]


def test_preview_endpoint_renders_the_measured_shape(project):
    file_id = store("stories.csv", b"name,count\nalpha,2\nbeta,3\n")

    response = client.get(preview_url(file_id))

    assert response.status_code == 200
    assert "stories.csv" in response.text
    assert "CSV" in response.text
    assert "2 rows" in response.text
    assert "<th>name</th><th>count</th>" in response.text
    assert '<td class="cell-clip" title="alpha">alpha</td>' in response.text


def test_preview_keeps_zero_padded_text(project):
    file_id = store("filings.csv", b"filing_id\n002\n010\n")

    preview = build_file_preview("demo", file_id)

    assert preview.rows == [["002"], ["010"]]


def test_preview_endpoint_names_a_bounded_result(project):
    values = "\n".join(str(index) for index in range(PREVIEW_ROWS_SHOWN + 1))
    file_id = store("long.csv", f"value\n{values}\n".encode())

    body = client.get(preview_url(file_id)).text

    assert f"First {PREVIEW_ROWS_SHOWN} of {PREVIEW_ROWS_SHOWN + 1} rows" in body
    assert f'title="{PREVIEW_ROWS_SHOWN - 1}"' in body
    assert f'title="{PREVIEW_ROWS_SHOWN}"' not in body


def test_preview_endpoint_escapes_file_data(project):
    file_id = store(
        "<script>alert(1)<script>.csv",
        b'name\n"<img src=x onerror=alert(1)>"\n',
    )

    body = client.get(preview_url(file_id)).text

    assert "<script>alert(1)" not in body
    assert "<img src=x" not in body
    assert "&lt;script&gt;alert(1)&lt;script&gt;.csv" in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body


def test_preview_endpoint_refuses_another_projects_file(project):
    file_id = store("other.csv", b"name\nother\n", "other")

    response = client.get(preview_url(file_id))

    assert response.status_code == 404
    assert "has no file" in response.text


def test_preview_endpoint_refuses_an_unknown_format(project):
    file_id = store("notes.txt", b"not a supported table")

    response = client.get(preview_url(file_id))

    assert response.status_code == 422
    assert "cannot tell what format" in response.text


def test_preview_endpoint_has_an_empty_file_state(project):
    file_id = store("empty.csv", b"name,count\n")

    body = client.get(preview_url(file_id)).text

    assert "0 rows" in body
    assert "This file has columns but no rows." in body


def test_preview_dialog_controller_handles_loading_failure_and_close():
    script = (Path(__file__).parents[1] / "app/static/file-preview.js").read_text(
        encoding="utf-8"
    )

    assert 'showMessage(body, "Loading preview…"' in script
    assert 'error.name !== "AbortError"' in script
    assert "request.abort()" in script
    assert 'dialog.addEventListener("cancel"' in script
    assert 'dialog.addEventListener("keydown"' in script
    assert 'event.key !== "Escape"' in script
    assert "event.preventDefault()" in script
    assert "event.target === dialog" in script
    assert "activeButton.focus()" in script
    assert "if (!fileId ||" in script
    assert "button.dataset.fileId" in script
    assert 'button.getAttribute("aria-disabled") === "true"' in script
    assert 'activePicker.classList.add("is-action-open")' in script
    assert 'activePicker.classList.remove("is-action-open")' in script

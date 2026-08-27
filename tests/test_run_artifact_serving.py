"""A run's artifact is served as its own file type; decoding a binary one as text is a 500."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def artifact_dir(tmp_path, monkeypatch):
    from app.services import workspace

    root = tmp_path / "proj" / "runs" / "R1"
    (root / "artifacts").mkdir(parents=True)
    workspace.set_projects_dir(tmp_path)
    return root / "artifacts"


def test_a_binary_artifact_is_served_rather_than_decoded(artifact_dir):
    body = b"PK\x03\x04\xd7\x00binary"  # not valid UTF-8; read_text would raise
    (artifact_dir / "book.xlsx").write_bytes(body)
    response = TestClient(app).get("/project/proj/runs/R1/artifact/book.xlsx")
    assert response.status_code == 200
    assert response.content == body


def test_an_html_artifact_is_still_served_inline(artifact_dir):
    (artifact_dir / "profile.html").write_text("<h1>hi</h1>", encoding="utf-8")
    response = TestClient(app).get("/project/proj/runs/R1/artifact/profile.html")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<h1>hi</h1>" in response.text


def test_a_missing_artifact_is_still_a_404(artifact_dir):
    assert TestClient(app).get("/project/proj/runs/R1/artifact/nope.xlsx").status_code == 404

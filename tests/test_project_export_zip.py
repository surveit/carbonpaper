"""GET /project/{project}/export.zip — the whole-directory download. The
endpoint archives the project's on-disk tree byte-for-byte (it parses nothing),
so the fixture stages the tree directly and the test opens the zip back up.
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

_STAGE = {
    "id": "load", "type": "input_data", "description": "Load documents",
    "connector": {"kind": "file", "params": {"format": "csv"}},
    "signature": {
        "form": "replaces",
        "produces": [{"name": "doc_id", "type": "str", "nullable": True}],
    },
}

_PARQUET_ISH = b"PAR1\x00\x15binary-not-utf8\xff\xfePAR1"


@pytest.fixture
def seeded_project(projects_root):
    """Stage one project tree; returns the paths the zip must contain."""
    pdir = projects_root / "demo"
    # Every kind of content the export covers: authored files, a run's
    # outputs/ and artifacts/, and an upload.
    (pdir / "compiled").mkdir(parents=True)
    (pdir / "compiled" / "01_load.json").write_text(
        json.dumps(_STAGE, indent=2), encoding="utf-8")
    (pdir / "schemas").mkdir()
    (pdir / "schemas" / "01_documents.json").write_text(
        json.dumps({"name": "documents", "kind": "input", "columns": []}),
        encoding="utf-8")
    (pdir / "document.md").write_text("Trace the shell companies.", encoding="utf-8")
    (pdir / "project.json").write_text(json.dumps({"title": "Demo"}), encoding="utf-8")
    (pdir / "versions").mkdir()
    (pdir / "versions" / "v1.json").write_text(json.dumps({"id": "v1"}), encoding="utf-8")
    run_dir = pdir / "runs" / "R1"
    (run_dir / "outputs").mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({"run_id": "R1"}), encoding="utf-8")
    (run_dir / "outputs" / "load.parquet").write_bytes(_PARQUET_ISH)
    (run_dir / "artifacts").mkdir()
    (run_dir / "artifacts" / "report.html").write_text("<h1>hi</h1>", encoding="utf-8")
    (pdir / "uploads").mkdir()
    (pdir / "uploads" / "docs.csv").write_text("doc_id\nd1\n", encoding="utf-8")
    return {
        "compiled/01_load.json",
        "schemas/01_documents.json",
        "document.md",
        "project.json",
        "versions/v1.json",
        "runs/R1/manifest.json",
        "runs/R1/outputs/load.parquet",
        "runs/R1/artifacts/report.html",
        "uploads/docs.csv",
    }


def test_export_zip_round_trips_the_whole_project_tree(seeded_project):
    response = client.get("/project/demo/export.zip")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert 'filename="demo-export.zip"' in response.headers["content-disposition"]

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert set(archive.namelist()) == seeded_project
    # Byte fidelity, on the binary member especially — the export copies files,
    # it never decodes them.
    assert archive.read("runs/R1/outputs/load.parquet") == _PARQUET_ISH
    assert archive.read("document.md") == b"Trace the shell companies."


def test_unknown_project_is_a_404(projects_root):
    assert client.get("/project/nope/export.zip").status_code == 404


def test_a_plain_file_under_the_root_is_not_a_project(projects_root):
    (projects_root / "loose.txt").write_text("not a project", encoding="utf-8")
    assert client.get("/project/loose.txt/export.zip").status_code == 404

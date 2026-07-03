"""Route smoke tests over the shipped examples: every page that renders stages
must work on Stage objects (not dicts). Uses lobbymap, the richest example."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_index():
    assert client.get("/").status_code == 200


def test_methodology_dag_page():
    r = client.get("/methodology/lobbymap")
    assert r.status_code == 200
    assert "evidence_extraction" in r.text


def test_stage_detail_page():
    r = client.get("/methodology/lobbymap/stage/evidence_extraction")
    assert r.status_code == 200
    assert "Extract evidence pieces" in r.text          # stage name rendered
    assert "You are reading a document" in r.text        # prompt template rendered


def test_stage_partial():
    assert client.get("/methodology/lobbymap/stage/evidence_extraction/partial").status_code == 200


def test_data_model_page():
    assert client.get("/methodology/lobbymap/data-model").status_code == 200


def test_raw_stage():
    r = client.get("/methodology/lobbymap/raw/evidence_extraction")
    assert r.status_code == 200
    assert "evidence_extraction" in r.text


def test_trigger_run_returns_400_on_invalid_dag(monkeypatch):
    from pathlib import Path

    from app.models.loader import MethodologyLoadError
    import app.web.routers.runs as runs_router

    def _boom(methodology_dir, repo_root):
        raise MethodologyLoadError(Path("compiled"), ["01_bad.yaml: params.path missing"])

    monkeypatch.setattr(runs_router, "prepare_run", _boom)
    r = client.post("/methodology/lobbymap/run")
    assert r.status_code == 400
    assert "01_bad.yaml: params.path missing" in r.json()["issues"]


def test_resume_returns_400_on_invalid_dag(monkeypatch, tmp_path):
    from pathlib import Path

    from app.models.loader import MethodologyLoadError
    import app.web.routers.runs as runs_router

    def _boom(methodology_dir):
        raise MethodologyLoadError(Path("compiled"), ["02_bad.yaml: unknown file format"])

    monkeypatch.setattr(runs_router, "load_methodology_stages", _boom)
    # use a real existing run of lobbymap if present; otherwise skip guard:
    runs = sorted((Path("examples/lobbymap/runs")).glob("*/manifest.json"))
    if not runs:
        import pytest
        pytest.skip("no existing lobbymap run on disk to resume against")
    run_id = runs[-1].parent.name
    r = client.post(f"/methodology/lobbymap/runs/{run_id}/resume")
    assert r.status_code == 400
    assert "02_bad.yaml: unknown file format" in r.json()["issues"]

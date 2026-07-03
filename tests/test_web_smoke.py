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

"""POST /project/{p}/version on an invalid working copy: issue #162's motivating
scenario — a workflow with a dropped stage used to hit an unhandled 500 with no
report at all, and even once caught, N downstream stages referencing the dropped
stage would report N flat 'references no stage' lines for what is really ONE
root cause. This asserts both are fixed: a 400 (not a 500) carrying ONE grouped
issue line, not one per cascaded downstream consumer."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    import app.web.loading as loading
    import app.web.routers.node_review as node_review_router
    monkeypatch.setattr(node_review_router, "EXAMPLES_DIR", tmp_path)
    monkeypatch.setattr(loading, "EXAMPLES_DIR", tmp_path)
    return TestClient(app)


def _seed_cascading_workflow(root: Path) -> None:
    compiled = root / "alpha" / "compiled"
    compiled.mkdir(parents=True)
    # The root cause: a removed connector kind (the exact #162 dogfood failure).
    (compiled / "01_bad.json").write_text(json.dumps({
        "id": "bad", "name": "Bad connector", "type": "input_data",
        "connector": {"kind": "api", "params": {}},
    }), encoding="utf-8")
    # 3 downstream stages that all consume the dropped stage — each would
    # independently report "references no stage `bad`" if not grouped.
    for i in range(3):
        (compiled / f"0{i + 2}_down{i}.json").write_text(json.dumps({
            "id": f"down{i}", "name": f"Down{i}", "type": "python_frame_function",
            "inputs": [{"id": "bad"}],
            "function": {"kind": "inline", "code": "def transform(row): return row"},
        }), encoding="utf-8")


def test_invalid_working_copy_returns_400_not_500(client: TestClient, tmp_path: Path) -> None:
    _seed_cascading_workflow(tmp_path)
    response = client.post("/project/alpha/version", data={"message": "v1"})
    assert response.status_code == 400
    assert not (tmp_path / "alpha" / "versions").exists()


def test_report_collapses_cascade_to_one_root_cause_line(client: TestClient, tmp_path: Path) -> None:
    _seed_cascading_workflow(tmp_path)
    response = client.post("/project/alpha/version", data={"message": "v1"})
    body = response.json()
    assert body["ok"] is False
    issues = body["issues"]
    # ONE line for the whole cascade (the broken file + who it broke), not
    # 1 (file) + 3 (one per downstream consumer) = 4.
    assert len(issues) == 1
    assert "01_bad.json" in issues[0]
    for i in range(3):
        assert f"down{i}" in issues[0]

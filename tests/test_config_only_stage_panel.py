"""A config-only stage panel carries no certification badge at all.

A union's inputs and a join's keys are the whole step — there is no description for a
badge to make a claim about, so the panel shows the settings and says nothing.
"""
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import workspace

_SCHEMA = {"columns": [{"name": "id", "type": "str", "nullable": False}]}
_SCORED = {"columns": [{"name": "id", "type": "str", "nullable": False},
                       {"name": "score", "type": "int", "nullable": False}]}


def _seed_project(root: Path) -> None:
    compiled = root / "alpha" / "compiled"
    compiled.mkdir(parents=True)
    stages: list[dict[str, Any]] = [
        {"id": "q1", "description": "Q1", "type": "input_data",
         "connector": {"kind": "file"}, "signature": {"form": "replaces", "produces": _SCHEMA["columns"]}},
        {"id": "q2", "description": "Q2", "type": "input_data",
         "connector": {"kind": "file"}, "signature": {"form": "replaces", "produces": _SCHEMA["columns"]}},
        {"id": "both", "description": "Both quarters", "type": "union",
         "inputs": [{"id": "q1", "schema": _SCHEMA}, {"id": "q2", "schema": _SCHEMA}],
         "signature": {"form": "replaces", "produces": _SCHEMA["columns"]}, "union": {}},
        {"id": "score", "description": "Score", "type": "llm_transform",
         "inputs": [{"id": "both", "schema": _SCHEMA}],
         "signature": {
             "form": "extends",
             "reads": [{"input": "both", "columns": _SCHEMA["columns"]}],
             "adds": [{"name": "score", "type": "int", "nullable": False}],
         },
         "llm": {"prompt_template": "score {id}"}},
    ]
    for position, stage in enumerate(stages, start=1):
        (compiled / f"{position:02d}_{stage['id']}.json").write_text(
            json.dumps(stage), encoding="utf-8")


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    workspace.set_projects_dir(tmp_path)
    _seed_project(tmp_path)
    return TestClient(app)


def test_a_union_gets_no_badge(client: TestClient) -> None:
    html = client.get("/project/alpha/node/both/panel").text
    assert "stage-cert" not in html
    assert "<h2>🧬 Union</h2>" in html


def test_an_llm_stage_gets_no_badge_either(client: TestClient) -> None:
    html = client.get("/project/alpha/node/score/panel").text
    assert "stage-cert" not in html

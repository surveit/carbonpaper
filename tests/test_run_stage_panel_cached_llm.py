"""The Data pane's `stat-strip` block over two runs of one llm_transform, model
stubbed: the first pays, the second is answered entirely by the row cache and so
writes no `llm_usage` at all. A block keyed off usage renders nothing for the
second, which is exactly what a stage with no model looks like."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.workspace as workspace
from app.core.agent.usage import LlmUsage
from app.main import app
from app.runtime.manifest import read_run_manifest, write_manifest
from app.runtime.runner import execute_run, prepare_run
from app.services import project as project_service
from conftest import pinned_stages
from stage_seed import add_stage

PROJECT = "cached_llm_panel"
_COLUMNS = [{"name": "x", "type": "int", "nullable": True}]


def _load_stage(data_path: Path) -> dict:
    return {
        "id": "load", "description": "Load rows", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(data_path), "format": "csv"}},
        "signature": {"form": "replaces", "produces": _COLUMNS},
    }


def _judge_stage() -> dict:
    return {
        "id": "judge", "description": "Judge each row", "type": "llm_transform",
        "inputs": [{"id": "load"}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": _COLUMNS}],
            "adds": [{"name": "verdict", "type": "str", "nullable": True}],
        },
        "llm": {"prompt_instructions": "judge it", "prompt_data_template": "{x}",
                "batch_size": 1},
    }


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pdir = tmp_path / PROJECT
    pdir.mkdir(parents=True, exist_ok=True)
    data = pdir / "rows.csv"
    pd.DataFrame({"x": [1, 2]}).to_csv(data, index=False)
    add_stage(pdir, _load_stage(data))
    add_stage(pdir, _judge_stage())
    workspace.set_projects_dir(tmp_path)

    def fake_call_llm(stage_id, llm, row, reply_model, usage_out):
        usage_out.append(LlmUsage(input_tokens=10, output_tokens=5, cost_usd=0.25, calls=1))
        return {"verdict": f"v{row['x']}"}

    monkeypatch.setattr("app.runtime.stages.llm_transform.call_llm", fake_call_llm)
    project_service.save_working_copy_as_version(pdir.name, message="v1")
    return pdir


def _run(project_dir: Path) -> str:
    return str(execute_run(project_dir / "runs", project_dir.name, *pinned_stages(project_dir))["run_id"])


def _panel(run_id: str) -> str:
    client = TestClient(app)
    response = client.get(f"/project/{PROJECT}/runs/{run_id}/stage/judge/partial")
    assert response.status_code == 200, response.text
    return response.text


def test_the_paying_run_still_reports_its_calls_and_cost(project: Path) -> None:
    html = _panel(_run(project))
    assert "stat-strip" in html
    assert "$0.50" in html  # two rows at the stub's $0.25 each
    assert "Reused, not recomputed" not in html


def test_the_replayed_run_says_so_where_the_cost_would_be(project: Path) -> None:
    _run(project)
    replayed = _panel(_run(project))
    assert "stat-strip" in replayed
    assert "Reused, not recomputed" in replayed
    assert "2 of 2 rows (100%)" in replayed
    assert "the model was not called for those cached rows" in replayed


def test_a_partially_replayed_run_limits_its_no_call_claim_to_cached_rows(project: Path) -> None:
    _run(project)
    pd.DataFrame({"x": [1, 2, 3]}).to_csv(project / "rows.csv", index=False)

    html = _panel(_run(project))

    assert "2 of 3 rows (67%)" in html
    assert "<dt>calls</dt><dd>1</dd>" in html
    assert "$0.25" in html
    assert "the model was not called for those cached rows" in html
    assert "the model was not called in this run" not in html


def test_a_restarted_pending_run_does_not_claim_cache_replay(project: Path) -> None:
    _run(project)
    pending = prepare_run(project / "runs", project.name, *pinned_stages(project))

    html = _panel(pending["run_id"])

    assert "Reused, not recomputed" not in html


def test_cache_replay_omits_a_percentage_without_output_rows(project: Path) -> None:
    _run(project)
    run_id = _run(project)
    manifest = read_run_manifest(project.name, run_id)
    record = manifest.find_stage_record("judge")
    assert record is not None
    record.output_row_count = 0
    write_manifest(manifest)

    html = _panel(run_id)

    assert "2 of 0 rows came back" in html
    assert "2 of 0 rows (" not in html


def test_the_replayed_run_names_the_model_it_did_not_call(project: Path) -> None:
    _run(project)
    replayed = _panel(_run(project))
    assert "<dt>model</dt>" in replayed
    assert "<dd>none</dd>" in replayed

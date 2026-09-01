"""The Data pane names the model from the RUN's manifest, and states the batch size as
a ceiling beside what the run actually did. The stage definition names one model and the
run records another, so a panel reading the definition renders the wrong id."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.workspace as workspace
from app.core.agent.usage import LlmUsage
from app.main import app
from app.runtime.runner import execute_run
from app.services import project as project_service
from conftest import pinned_stages
from stage_seed import add_stage

PROJECT = "model_provenance_panel"
_COLUMNS = [{"name": "x", "type": "int", "nullable": True}]
# What the stage ASKS FOR, and what the stubbed backend records as having ANSWERED.
_ASKED_MODEL = "claude-opus-5"
_ANSWERED_MODEL = "claude-haiku-4-5"
_BATCH_SIZE = 12
_ROWS = 5


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
                "model": _ASKED_MODEL, "batch_size": _BATCH_SIZE},
    }


def _build_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answered: str | None
) -> Path:
    pdir = tmp_path / PROJECT
    pdir.mkdir(parents=True, exist_ok=True)
    data = pdir / "rows.csv"
    pd.DataFrame({"x": list(range(_ROWS))}).to_csv(data, index=False)
    add_stage(pdir, _load_stage(data))
    add_stage(pdir, _judge_stage())
    workspace.set_projects_dir(tmp_path)

    def fake_call_llm_batch(stage_id, llm, *, instructions, task, reply_schema, usage_out):
        usage_out.append(LlmUsage(cost_usd=0.25, calls=1, model=answered))
        return {"results": [{"row_number": n, "verdict": f"v{n}"}
                            for n in range(task.count("### item "))]}

    monkeypatch.setattr(
        "app.runtime.stages.llm_transform.call_llm_batch", fake_call_llm_batch)
    project_service.save_working_copy_as_version(pdir.name, message="v1")
    return pdir


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return _build_project(tmp_path, monkeypatch, _ANSWERED_MODEL)


@pytest.fixture()
def project_run_before_the_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """What a manifest written before the runtime recorded the model parses back as."""
    return _build_project(tmp_path, monkeypatch, None)


def _panel(project_dir: Path) -> str:
    run_id = str(execute_run(project_dir / "runs", project_dir.name, *pinned_stages(project_dir))["run_id"])
    client = TestClient(app)
    response = client.get(f"/project/{PROJECT}/runs/{run_id}/stage/judge/partial")
    assert response.status_code == 200, response.text
    return response.text


def _stat_strip(html: str) -> str:
    start = html.index('<dl class="stat-strip">')
    return html[start:html.index("</dl>", start)]


def test_the_panel_names_the_model_the_run_recorded(project: Path) -> None:
    strip = _stat_strip(_panel(project))
    assert f"<code>{_ANSWERED_MODEL}</code>" in strip
    # Scoped to the strip: the Transform tab still shows the definition's `model`,
    # which is the model that was asked for rather than the one that answered.
    assert _ASKED_MODEL not in strip


def test_the_batch_size_is_stated_as_a_ceiling(project: Path) -> None:
    assert f"up to {_BATCH_SIZE}" in _panel(project)


def test_the_disclosure_counts_what_the_run_actually_did(project: Path) -> None:
    html = _panel(project)
    # One call held all five rows, so the configured 12 describes no call this run made.
    assert f"{_ROWS} rows" in html
    assert "1 model call" in html


def test_the_disclosure_still_names_the_contamination_risk(project: Path) -> None:
    assert "swayed by the others in its call" in _panel(project)


def test_a_run_that_recorded_no_model_says_so_rather_than_borrowing_one(
    project_run_before_the_field: Path,
) -> None:
    strip = _stat_strip(_panel(project_run_before_the_field))
    assert "not recorded" in strip
    assert _ASKED_MODEL not in strip

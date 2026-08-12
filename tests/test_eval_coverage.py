"""Whether an eval vouches for one stage (app/web/eval_coverage.py), and the badge the
stage panel draws from it. Seeds one eval targeting `classify` plus one scored run, and
varies the version the reader is looking at — the stale path is the one live data cannot
reach, because a project's newest eval run is normally against its newest version.
"""
from __future__ import annotations

import json
from typing import Literal

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.core.frames import write_frame_file
from app.main import app
from app.models import EvalConfig, EvalRun, EvalRunSettings, ExpectedOutput
from app.evals.store import save_eval_config, save_eval_run
from app.services import workspace
from app.web.eval_coverage import find_eval_coverage

client = TestClient(app)

def _load_stage(tmp_path):
    # Absolute: one invalid compiled file makes load_stages return no stages at all.
    return {
        "id": "load", "type": "input_data", "description": "Load documents",
        "connector": {"kind": "file",
                      "params": {"path": str(tmp_path / "docs.csv"), "format": "csv"}},
        "signature": {"form": "replaces", "produces": [
            {"name": "doc_id", "type": "str", "nullable": True},
            {"name": "text", "type": "str", "nullable": True}]},
    }
_CLASSIFY = {
    "id": "classify", "type": "python_row_function", "description": "Classify each row",
    "inputs": [{"id": "load"}],
    "function": {"kind": "inline", "code": "def transform(row): return row"},
    "signature": {"form": "extends", "reads": [{"input": "load", "columns": [
        {"name": "doc_id", "type": "str", "nullable": True},
        {"name": "text", "type": "str", "nullable": True}]}],
        "adds": [{"name": "label", "type": "str", "nullable": True}]},
}

# One row matched, one did not — so a single fixture serves both the pass and the
# mismatch case, and the badge's arithmetic is visible in the numbers it prints.
_ONE_OF_TWO_MATCHED = pd.DataFrame({
    "label__expected": ["x", "y"], "label__actual": ["x", "z"],
    "label__match": [True, False], "row_passed": [True, False],
})
_BOTH_MATCHED = pd.DataFrame({
    "label__expected": ["x", "y"], "label__actual": ["x", "y"],
    "label__match": [True, True], "row_passed": [True, True],
})


@pytest.fixture(autouse=True)
def demo_project(tmp_path):
    demo = tmp_path / "demo"
    compiled = demo / "compiled"
    compiled.mkdir(parents=True)
    (compiled / "01_load.json").write_text(json.dumps(_load_stage(tmp_path)), encoding="utf-8")
    (compiled / "02_classify.json").write_text(json.dumps(_CLASSIFY), encoding="utf-8")
    workspace.set_projects_dir(tmp_path)
    save_eval_config(demo, EvalConfig(
        id="label_check", project="demo", name="Label check",
        override_stage="load", target_stage="classify",
        expected_outputs=[ExpectedOutput(output_column="label", metric="exact")]))
    return tmp_path


def _save_run(tmp_path, per_row: pd.DataFrame, *, version: str = "v1", run_id: str = "r1",
              status: Literal["scored", "vetoed", "error"] = "scored") -> None:
    result = tmp_path / "demo" / "eval_run" / run_id / "result.parquet"
    result.parent.mkdir(parents=True, exist_ok=True)
    write_frame_file(per_row, result)
    save_eval_run(tmp_path / "demo", EvalRun(
        id=run_id, config="label_check", project="demo", workflow_version=version,
        status=status,
        settings=EvalRunSettings(can_score_declaratively=True, frontier=["classify"],
                                 blocking_stages=[]),
        result_ref=f"eval_run/{run_id}/result.parquet",
        started_at="2026-08-12T10:00:00", finished_at="2026-08-12T10:00:20"))


# ── The verdict ──────────────────────────────────────────────────────────────

def test_a_run_against_the_version_being_read_vouches_for_the_stage(tmp_path):
    _save_run(tmp_path, _BOTH_MATCHED)

    coverage = find_eval_coverage("demo", "classify", "v1")

    assert coverage is not None
    assert coverage.status == "checked"
    assert (coverage.rows_passed, coverage.rows_total) == (2, 2)
    assert coverage.columns == ["label"]


def test_a_run_against_another_version_is_stale_however_it_scored(tmp_path):
    _save_run(tmp_path, _BOTH_MATCHED, version="v1")

    coverage = find_eval_coverage("demo", "classify", "v2")

    assert coverage is not None
    assert coverage.status == "stale"
    # The score still travels, so the badge can say what the older result WAS.
    assert (coverage.rows_passed, coverage.rows_total) == (2, 2)
    assert coverage.scored_version == "v1"


def test_an_unresolvable_version_is_stale_not_a_verdict(tmp_path):
    _save_run(tmp_path, _BOTH_MATCHED)

    assert find_eval_coverage("demo", "classify", None).status == "stale"


def test_a_row_that_did_not_match_is_reported_as_a_mismatch(tmp_path):
    _save_run(tmp_path, _ONE_OF_TWO_MATCHED)

    coverage = find_eval_coverage("demo", "classify", "v1")

    assert coverage.status == "mismatches"
    assert (coverage.rows_passed, coverage.rows_total) == (1, 2)


def test_coverage_attaches_to_the_target_stage_only(tmp_path):
    _save_run(tmp_path, _BOTH_MATCHED)

    # `load` is the eval's OVERRIDE — it was replaced, not checked.
    assert find_eval_coverage("demo", "load", "v1") is None


def test_a_stage_no_eval_targets_carries_nothing(tmp_path):
    _save_run(tmp_path, _BOTH_MATCHED)

    assert find_eval_coverage("demo", "nobody_evals_this", "v1") is None


def test_a_run_that_never_scored_vouches_for_nothing(tmp_path):
    _save_run(tmp_path, _BOTH_MATCHED, run_id="vetoed1", status="vetoed")

    assert find_eval_coverage("demo", "classify", "v1") is None


def test_a_missing_result_table_states_nothing_rather_than_guessing(tmp_path):
    _save_run(tmp_path, _BOTH_MATCHED)
    (tmp_path / "demo" / "eval_run" / "r1" / "result.parquet").unlink()

    assert find_eval_coverage("demo", "classify", "v1") is None


# ── The badge the node panel draws ───────────────────────────────────────────

def test_the_node_panel_carries_the_badge_for_a_checked_stage(tmp_path):
    from app.services.versioning import WorkflowVersion
    WorkflowVersion(id="demo/v1", version_id="v1", created_at="2026-08-12T00:00:00",
                    message="m", reviewer="r").save()
    _save_run(tmp_path, _BOTH_MATCHED)

    r = client.get("/project/demo/node/classify/panel")

    assert r.status_code == 200
    assert "stage-eval-check" in r.text
    assert "Matches 2 of 2 hand-labelled cases" in r.text


def test_the_node_panel_never_shows_a_stale_score_in_the_badge_line(tmp_path):
    from app.services.versioning import WorkflowVersion
    WorkflowVersion(id="demo/v2", version_id="v2", created_at="2026-08-12T00:00:00",
                    message="m", reviewer="r").save()
    _save_run(tmp_path, _BOTH_MATCHED, version="v1")

    r = client.get("/project/demo/node/classify/panel")

    badge = r.text.split('class="stage-cert-badge"')[1].split("</span>")[0]
    assert "Not checked since this step changed" in badge
    assert "2 of 2" not in badge          # the number is in the explanation, not the verdict
    assert "v1" in r.text                 # which version it WAS about is still named


def test_the_node_panel_of_an_unevaluated_stage_carries_no_badge(tmp_path):
    _save_run(tmp_path, _BOTH_MATCHED)

    r = client.get("/project/demo/node/load/panel")

    assert r.status_code == 200
    assert "stage-eval-check" not in r.text

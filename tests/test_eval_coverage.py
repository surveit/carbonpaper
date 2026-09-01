"""Whether an eval vouches for one stage (app/web/eval_coverage.py), and the badge the
stage panel draws from it. Seeds one eval targeting `classify` plus one scored run, and
varies the version the reader is looking at — the stale path is the one live data cannot
reach, because a project's newest eval run is normally against its newest version.
"""
from __future__ import annotations

from typing import Literal

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.core.frames import write_frame_file
from app.main import app
from app.models import EvalRunSettings, ExpectedOutput
from app.models.records.eval_config import EvalConfig
from app.models.records.eval_run import EvalRun
from app.evals.store import save_eval_config, save_eval_run
from app.services import workspace
from app.web.eval_coverage import find_eval_coverages
from stage_seed import add_stage

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
    compiled = demo
    compiled.mkdir(parents=True, exist_ok=True)
    add_stage(compiled, _load_stage(tmp_path))
    add_stage(compiled, _CLASSIFY)
    workspace.set_projects_dir(tmp_path)
    save_eval_config(demo.name, EvalConfig(
        eval_id="label_check", project="demo", name="Label check",
        override_stage="load", target_stage="classify",
        expected_outputs=[ExpectedOutput(output_column="label", metric="exact")]))
    return tmp_path


def _save_run(tmp_path, per_row: pd.DataFrame, *, version: str = "v1", run_id: str = "r1",
              status: Literal["scored", "vetoed", "error"] = "scored") -> None:
    result = tmp_path / "demo" / "eval_run" / run_id / "result.parquet"
    result.parent.mkdir(parents=True, exist_ok=True)
    write_frame_file(per_row, result)
    save_eval_run("demo", EvalRun(
        run_id=run_id, config="label_check", project="demo", workflow_version=version,
        status=status,
        settings=EvalRunSettings(can_score_declaratively=True, frontier=["classify"],
                                 blocking_stages=[]),
        result_ref=f"eval_run/{run_id}/result.parquet",
        started_at="2026-08-12T10:00:00", finished_at="2026-08-12T10:00:20"))


def _only(coverages):
    assert len(coverages) == 1, f"expected one eval, got {len(coverages)}"
    return coverages[0]


# ── The verdict ──────────────────────────────────────────────────────────────

def test_a_run_against_the_version_being_read_vouches_for_the_stage(tmp_path):
    _save_run(tmp_path, _BOTH_MATCHED)

    coverage = _only(find_eval_coverages("demo", "classify", "v1"))

    assert coverage.status == "checked"
    assert (coverage.rows_passed, coverage.rows_total) == (2, 2)
    assert coverage.columns == ["label"]


def test_a_run_against_another_version_is_stale_however_it_scored(tmp_path):
    _save_run(tmp_path, _BOTH_MATCHED, version="v1")

    coverage = _only(find_eval_coverages("demo", "classify", "v2"))

    assert coverage.status == "stale"
    # The score still travels, so the badge can say what the older result WAS.
    assert (coverage.rows_passed, coverage.rows_total) == (2, 2)
    assert coverage.scored_version == "v1"


def test_an_unresolvable_version_is_stale_not_a_verdict(tmp_path):
    _save_run(tmp_path, _BOTH_MATCHED)

    assert _only(find_eval_coverages("demo", "classify", None)).status == "stale"


def test_a_row_that_did_not_match_is_reported_as_a_mismatch(tmp_path):
    _save_run(tmp_path, _ONE_OF_TWO_MATCHED)

    coverage = _only(find_eval_coverages("demo", "classify", "v1"))

    assert coverage.status == "mismatches"
    assert (coverage.rows_passed, coverage.rows_total) == (1, 2)


def test_coverage_attaches_to_the_target_stage_only(tmp_path):
    _save_run(tmp_path, _BOTH_MATCHED)

    # `load` is the eval's OVERRIDE — it was replaced, not checked.
    assert find_eval_coverages("demo", "load", "v1") == []


def test_a_stage_no_eval_targets_carries_nothing(tmp_path):
    _save_run(tmp_path, _BOTH_MATCHED)

    assert find_eval_coverages("demo", "nobody_evals_this", "v1") == []


def test_a_run_that_never_scored_vouches_for_nothing(tmp_path):
    _save_run(tmp_path, _BOTH_MATCHED, run_id="vetoed1", status="vetoed")

    assert find_eval_coverages("demo", "classify", "v1") == []


def test_a_missing_result_table_states_nothing_rather_than_guessing(tmp_path):
    _save_run(tmp_path, _BOTH_MATCHED)
    (tmp_path / "demo" / "eval_run" / "r1" / "result.parquet").unlink()

    assert find_eval_coverages("demo", "classify", "v1") == []


# ── The section the Transform pane draws ────────────────────────────────────

def _stored_version(version_id: str) -> None:
    from app.models.records.workflow_version import WorkflowVersion
    WorkflowVersion(id=f"demo/{version_id}", version_id=version_id,
                    created_at="2026-08-12T00:00:00", message="m").save()


def _eval_section(html: str) -> str:
    """The subsection alone: an h3 inside a transform block, not a section of its own."""
    return html.split("worked examples (evals)")[1].split("</table>")[0]


def test_the_pane_reports_a_checked_stage_with_its_score(tmp_path):
    _stored_version("v1")
    _save_run(tmp_path, _BOTH_MATCHED)

    section = _eval_section(client.get("/project/demo/node/classify/panel").text)

    assert "verdict-pass" in section and "2 / 2" in section
    assert "Label check" in section and "label" in section


def test_a_stale_row_states_no_score_and_names_the_version_it_scored(tmp_path):
    _stored_version("v2")
    _save_run(tmp_path, _BOTH_MATCHED, version="v1")

    section = _eval_section(client.get("/project/demo/node/classify/panel").text)
    result_cell = section.split("<tbody>")[1].split("</td>")[0]

    assert "verdict-stale" in result_cell and "stale" in result_cell
    assert "2 / 2" not in result_cell          # a verdict on code that has moved
    assert "v1" in section                     # which version it WAS about is named


def test_two_evals_on_one_stage_get_a_row_each_worst_first(tmp_path):
    _stored_version("v1")
    save_eval_config("demo", EvalConfig(
        eval_id="second_check", project="demo", name="Second check",
        override_stage="load", target_stage="classify",
        expected_outputs=[ExpectedOutput(output_column="label", metric="exact")]))
    _save_run(tmp_path, _BOTH_MATCHED, run_id="passing")
    _save_run(tmp_path, _ONE_OF_TWO_MATCHED, run_id="failing")
    # The second config's run, so both evals have one of their own.
    save_eval_run("demo", EvalRun(
        run_id="failing", config="second_check", project="demo", workflow_version="v1",
        status="scored",
        settings=EvalRunSettings(can_score_declaratively=True, frontier=["classify"],
                                 blocking_stages=[]),
        result_ref="eval_run/failing/result.parquet",
        started_at="2026-08-12T10:00:00", finished_at="2026-08-12T10:00:20"))

    coverages = find_eval_coverages("demo", "classify", "v1")
    section = _eval_section(client.get("/project/demo/node/classify/panel").text)

    # Two datasets, so two rows — never one summed figure neither eval measured.
    assert [c.status for c in coverages] == ["mismatches", "checked"]
    assert section.split("<tbody>")[1].count("<tr>") == 2   # the header row is not one
    assert section.index("Second check") < section.index("Label check")


def test_a_stage_no_eval_targets_gets_no_section(tmp_path):
    _save_run(tmp_path, _BOTH_MATCHED)

    assert "worked examples (evals)" not in client.get(
        "/project/demo/node/load/panel").text


def test_the_llm_block_reads_in_the_order_one_call_happens(tmp_path):
    """Ask → what it sees → what shape comes back → what that scored → the dials."""
    llm = {
        "id": "arbiter", "type": "llm_transform", "description": "Judge each row",
        "inputs": [{"id": "load"}],
        "llm": {"prompt_instructions": "Decide.", "prompt_data_template": "{text}",
                "model": "claude-sonnet-5"},
        "signature": {"form": "extends", "reads": [{"input": "load", "columns": [
            {"name": "text", "type": "str", "nullable": True}]}],
            "adds": [{"name": "label", "type": "str", "nullable": True}]},
    }
    add_stage(tmp_path / "demo", llm)
    save_eval_config("demo", EvalConfig(
        eval_id="arbiter_check", project="demo", name="Arbiter check",
        override_stage="load", target_stage="arbiter",
        expected_outputs=[ExpectedOutput(output_column="label", metric="exact")]))
    _stored_version("v1")
    save_eval_run("demo", EvalRun(
        run_id="ar1", config="arbiter_check", project="demo", workflow_version="v1",
        status="scored",
        settings=EvalRunSettings(can_score_declaratively=True, frontier=["arbiter"],
                                 blocking_stages=[]),
        result_ref="eval_run/r1/result.parquet"))
    _save_run(tmp_path, _BOTH_MATCHED)

    html = client.get("/project/demo/node/arbiter/panel").text

    headings = ["what the model is asked", "what it sees, per row",
                "expected answer shape", "worked examples (evals)", "settings"]
    positions = [html.index(f"<h3>{h}</h3>") for h in headings]
    assert positions == sorted(positions), dict(zip(headings, positions))

"""A stage's real output: a workflow test executes the stages you name over the slice
you ask for, and profile_stage_output_data_range profiles several of that output's
columns at once — never presenting a cut list as a whole vocabulary."""
from __future__ import annotations

import pandas as pd
import pytest

from app.core.errors import StageNotInRun, StageOutputMissing
from app.models import parse_stage
from app.services import workspace
from app.services.run import read_run_status, read_stage_output
from app.models.records.workflow_version import WorkflowVersion
from app.services.workflow_test import run_workflow_test

# One distinct doc_id per row, 3 statuses in equal parts, and a numeric column
# spanning zero — so a range summary is distinguishable from a value list.
_ROW_COUNT = 24
_STATUSES = ["awarded", "protested", "cancelled"]
_ROWS = pd.DataFrame({
    "doc_id": [f"{n:03d}" for n in range(1, _ROW_COUNT + 1)],
    "status": [_STATUSES[n % 3] for n in range(_ROW_COUNT)],
    "score": [n - 2 for n in range(_ROW_COUNT)],
})

_LOAD_SCHEMA = {"columns": [{"name": "doc_id", "type": "str", "nullable": True},
                            {"name": "status", "type": "str", "nullable": True},
                            {"name": "score", "type": "int", "nullable": True}]}

_CLASSIFY = {
    "id": "classify", "type": "python_row_function", "description": "Label by sign",
    "inputs": [{"id": "load"}],
    # Carries `score` through: an `extends` signature flows every anchor column,
    # so a row function cannot drop one.
    "function": {"kind": "inline", "code":
                 "def transform(row):\n"
                 "    return {**row,\n"
                 "            'label': 'pos' if row['score'] >= 0 else 'neg'}"},
    "signature": {"form": "extends",
                  "reads": [{"input": "load", "columns": _LOAD_SCHEMA["columns"]}],
                  "adds": [{"name": "label", "type": "str", "nullable": True}]},
}


@pytest.fixture
def demo(tmp_path):
    workspace.set_projects_dir(tmp_path)
    project = tmp_path / "demo"
    (project / "data").mkdir(parents=True)
    source = project / "data" / "rows.csv"
    _ROWS.to_csv(source, index=False)
    load = {
        "id": "load", "type": "input_data", "description": "Load rows",
        "connector": {"kind": "file", "params": {"path": str(source), "format": "csv"}},
        "signature": {"form": "replaces", "produces": _LOAD_SCHEMA["columns"]},
    }
    WorkflowVersion(
        id="demo/v1", version_id="v1", created_at="2026-08-01T00:00:00",
        message="seed",
        stages=[parse_stage(load), parse_stage(_CLASSIFY)],
    ).save()
    return project


# ── 1. Which stages run, and over how much ──────────────────────────────────


def test_naming_the_input_stage_executes_it_over_the_whole_bound_file(demo):
    result = run_workflow_test("demo", stage_ids=["load"])
    assert result["ok"] is True
    assert result["stages_run"] == ["load"]
    assert len(read_stage_output("demo", result["run_id"], "load")) == _ROW_COUNT


def test_a_test_naming_no_stages_injects_the_slice_and_skips_the_input(demo):
    result = run_workflow_test("demo", limit=2)
    assert result["stages_run"] == ["classify"]
    assert len(read_stage_output("demo", result["run_id"], "classify")) == 2


def test_naming_a_stage_still_injects_the_slice_its_producer_owes_it(demo):
    result = run_workflow_test("demo", stage_ids=["classify"], limit=5)
    assert result["ok"] is True
    assert len(read_stage_output("demo", result["run_id"], "classify")) == 5


def test_an_omitted_limit_injects_the_whole_source(demo):
    result = run_workflow_test("demo", stage_ids=["classify"])
    assert len(read_stage_output("demo", result["run_id"], "classify")) == _ROW_COUNT


@pytest.mark.parametrize("stage_ids", [None, ["load", "classify"]], ids=["injected", "executed"])
def test_the_window_is_the_same_rows_whether_the_source_is_injected_or_executed(
    demo, stage_ids
):
    result = run_workflow_test("demo", stage_ids=stage_ids, limit=3, offset=2)
    frame = read_stage_output("demo", result["run_id"], "classify")
    assert list(frame["doc_id"]) == ["003", "004", "005"]


def test_a_source_stage_that_executes_notes_the_cut_it_took(demo):
    result = run_workflow_test("demo", stage_ids=["load"], limit=3)
    record = next(
        stage for stage in read_run_status("demo", result["run_id"])["stage_records"]
        if stage["stage_id"] == "load"
    )
    assert any("limit=3" in note for note in record["notes"])


def test_naming_an_unknown_stage_names_the_real_ones(demo):
    with pytest.raises(ValueError, match=r"classify.*load|load.*classify"):
        run_workflow_test("demo", stage_ids=["laod"])


# ── 2. Read a stage's output ────────────────────────────────────────────────


def test_reading_a_stage_output_honours_the_declared_dtype(demo):
    result = run_workflow_test("demo", stage_ids=["load"])
    frame = read_stage_output("demo", result["run_id"], "load")
    assert list(frame["doc_id"]) == list(_ROWS["doc_id"])
    assert frame["doc_id"][0] == "001"
    assert pd.read_csv(demo / "data" / "rows.csv")["doc_id"][0] == 1


def test_reading_a_stage_the_run_does_not_have_names_the_ones_it_ran(demo):
    result = run_workflow_test("demo", stage_ids=["load"])
    with pytest.raises(StageNotInRun, match="load"):
        read_stage_output("demo", result["run_id"], "classify")


def test_reading_a_stage_that_wrote_no_output_is_loud(demo):
    bad = dict(_CLASSIFY, function={"kind": "inline", "code":
                                    "def transform(row):\n    raise ValueError('boom')"})
    WorkflowVersion(
        id="demo/v2", version_id="v2", created_at="2026-08-02T00:00:00",
        message="broken",
        stages=[parse_stage(s) for s in (_load_stage(demo), bad)],
    ).save()
    result = run_workflow_test("demo", version_id="v2", limit=2)
    assert result["ok"] is False
    with pytest.raises(StageOutputMissing, match="no output"):
        read_stage_output("demo", result["run_id"], "classify")


def _load_stage(project) -> dict:
    return {
        "id": "load", "type": "input_data", "description": "Load rows",
        "connector": {"kind": "file", "params": {
            "path": str(project / "data" / "rows.csv"), "format": "csv"}},
        "signature": {"form": "replaces", "produces": _LOAD_SCHEMA["columns"]},
    }


# ── 3. The tool: several columns at once, never a cut list as a whole set ────


def _profile(project_id, run_id, stage_id, columns, max_values=100):
    from app.mcp import server

    return server.profile_stage_output_data_range(
        project_id=project_id, run_id=run_id, stage_id=stage_id,
        columns=columns, max_values=max_values)


def test_the_tool_profiles_several_columns_in_one_call(demo):
    run_id = run_workflow_test("demo", stage_ids=["load"])["run_id"]
    result = _profile("demo", run_id, "load", ["status", "score"])
    assert result["ok"] is True
    assert result["row_count"] == _ROW_COUNT
    assert [column["column"] for column in result["columns"]] == ["status", "score"]


def test_a_categorical_column_reports_a_count_per_value(demo):
    run_id = run_workflow_test("demo", stage_ids=["load"])["run_id"]
    status = _profile("demo", run_id, "load", ["status"])["columns"][0]
    assert status["values"] == [
        {"value": "awarded", "count": 8},
        {"value": "cancelled", "count": 8},
        {"value": "protested", "count": 8},
    ]
    assert status["distinct_count"] == 3
    assert status["truncated"] is False
    assert status["null_count"] == 0
    assert status["value_range"] is None


def test_a_numeric_column_reports_its_range(demo):
    run_id = run_workflow_test("demo", stage_ids=["load"])["run_id"]
    score = _profile("demo", run_id, "load", ["score"])["columns"][0]
    assert score["value_range"] == {
        "min": -2.0, "max": float(_ROW_COUNT - 3),
        "mean": float(_ROWS["score"].mean()), "median": float(_ROWS["score"].median()),
    }
    assert score["distinct_count"] == _ROW_COUNT


def test_a_cut_list_reports_the_true_distinct_count_beside_it(demo):
    run_id = run_workflow_test("demo", stage_ids=["load"])["run_id"]
    profile = _profile("demo", run_id, "load", ["doc_id"], max_values=2)["columns"][0]
    assert len(profile["values"]) == 2
    assert profile["distinct_count"] == _ROW_COUNT
    assert profile["truncated"] is True


def test_raising_max_values_returns_the_whole_vocabulary(demo):
    run_id = run_workflow_test("demo", stage_ids=["load"])["run_id"]
    profile = _profile("demo", run_id, "load", ["doc_id"], max_values=500)["columns"][0]
    assert profile["distinct_count"] == len(profile["values"]) == _ROW_COUNT
    assert profile["truncated"] is False


def test_an_unknown_column_names_the_columns_the_output_has(demo):
    run_id = run_workflow_test("demo", stage_ids=["load"])["run_id"]
    result = _profile("demo", run_id, "load", ["status", "stauts"])
    assert result["ok"] is False
    assert "stauts" in result["error"] and "status" in result["error"]
    assert "columns" not in result


def test_an_unknown_run_comes_back_as_a_loud_verdict(demo):
    result = _profile("demo", "20990101T000000", "load", ["status"])
    assert result["ok"] is False
    assert result["error"]


def test_a_column_a_stage_computes_is_profilable_though_no_file_holds_it(demo):
    run_id = run_workflow_test("demo", limit=6)["run_id"]
    profile = _profile("demo", run_id, "classify", ["label"])["columns"][0]
    assert [value["value"] for value in profile["values"]] == ["pos", "neg"]


def test_the_tool_is_registered_on_the_mcp_surface(demo):
    from app.mcp import server

    assert "profile_stage_output_data_range" in {
        tool.name for tool in server.mcp._tool_manager.list_tools()}


# ── The shared guidance rides both authoring surfaces ────────────────────────


def test_both_authoring_surfaces_carry_the_enum_from_data_guidance():
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT
    from app.mcp.server import INSTRUCTIONS
    from app.tools.prompt_fragments import ENUM_FROM_DATA_GUIDANCE

    assert ENUM_FROM_DATA_GUIDANCE in EDITING_SYSTEM_PROMPT
    assert ENUM_FROM_DATA_GUIDANCE in INSTRUCTIONS


def test_the_guidance_keeps_the_two_questions_and_the_sample_warning():
    from app.models.authoring_lifecycle_note import AUTHORING_LIFECYCLE_GUIDANCE
    from app.tools.prompt_fragments import ENUM_FROM_DATA_GUIDANCE

    text = ENUM_FROM_DATA_GUIDANCE
    assert "GENERATION" in text and "thousands of values and still be closed" in text
    assert "Do WE consume it as a discrete set" in text and "MANDATORY" in text
    assert "distinct COUNT is evidence, never the criterion" in text
    assert "goes in the PLAN" in text
    assert "SAMPLE, not the set" in text
    # No tool name in the shared text: the editing agent registers no run tool, so
    # each surface states its own recipe after embedding this.
    for name in ("profile_stage_output_data_range", "run_workflow_test", "save_version"):
        assert name not in text
    assert AUTHORING_LIFECYCLE_GUIDANCE not in text


def test_the_editing_prompt_asks_no_human_to_check_a_column(demo):
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT

    assert "ask the human" not in EDITING_SYSTEM_PROMPT

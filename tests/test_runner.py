from __future__ import annotations

import json
import time

import pandas as pd
import pytest

from app import cli
from app.core.errors import NoVersionToRunError, SubsetRunError
from app.services import run as run_service
from app.core.run_status import RunStatus
from app.models import parse_stage, Workflow
from app.runtime.runner import execute_run, resume_run
from app.runtime.executor import _raise_if_run_failed, run_subset
from app.models.run_manifest import RunManifest
from app.runtime.trace import trace_row
from app.runtime.stages import llm_transform as lt
from app.services.loader import WorkflowLoadError
from app.services import versioning
from app.services.project import save_working_copy_as_version
from app.services.versioning import list_versions
from conftest import pinned_stages, resumed_stages


# The two shapes every fixture in this file loads: the (name, val) items csv and
# the (id, text) csv an llm_transform scores. Declared once so an upstream's
# output_schema and its downstream's input `schema` cannot drift apart.
_NAME_VAL_SCHEMA = {"columns": [{"name": "name", "type": "str", "nullable": True},
                                {"name": "val", "type": "int", "nullable": True}]}
_ID_TEXT_SCHEMA = {"columns": [{"name": "id", "type": "str", "nullable": True},
                               {"name": "text", "type": "str", "nullable": True}]}


def _seed_version(root):
    return save_working_copy_as_version(root, message="test seed", reviewer="test").version_id


def _make_project(root):
    (root / "compiled").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    pd.DataFrame({"name": [f"row{i}" for i in range(5)], "val": list(range(5))}) \
        .to_csv(root / "data" / "items.csv", index=False)
    stage = {
        "id": "load", "description": "Load items", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(root / "data" / "items.csv"), "format": "csv"}},
        "signature": {"form": "replaces", "produces": _NAME_VAL_SCHEMA["columns"]},
        "limit": 2,
    }
    (root / "compiled" / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")


def test_limit_on_a_source_stage_caps_the_rows_it_loads(tmp_path):
    _make_project(tmp_path)
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path))

    assert manifest["status"] == "ok"
    [rec] = manifest["stage_records"]
    assert rec["status"] == "ok"
    assert rec["output_row_count"] == 2                        # 2 of the file's 5 rows
    assert any(n.startswith("limit=2") for n in rec.get("notes", []))   # not silent

    run_dir = tmp_path / "runs" / manifest["run_id"]
    out = pd.read_parquet(run_dir / "outputs" / "load.parquet")
    assert len(out) == 2

    on_disk = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk["run_id"] == manifest["run_id"]
    assert on_disk["status"] == "ok"


def test_per_run_limit_and_offset_slice_and_are_recorded(tmp_path):
    # Offset applies BEFORE the cap: offset 1 skips row 0, then limit 3 reads rows 1-3.
    _make_project(tmp_path)
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path),
                           limits={"load": 3}, offsets={"load": 1})

    [rec] = manifest["stage_records"]
    assert rec["output_row_count"] == 3                                   # not the static 2
    out = pd.read_parquet(
        tmp_path / "runs" / manifest["run_id"] / "outputs" / "load.parquet")
    assert list(out["val"]) == [1, 2, 3]

    # The slice is part of the run's provenance: recorded on the manifest
    # and noted on the stage record, never silent.
    assert manifest["parameters"]["limits"] == {"load": 3}
    assert manifest["parameters"]["offsets"] == {"load": 1}
    notes = rec.get("notes", [])
    assert any(n.startswith("offset=1") for n in notes)
    assert any(n.startswith("limit=3") for n in notes)

    on_disk = json.loads(
        (tmp_path / "runs" / manifest["run_id"] / "manifest.json")
        .read_text(encoding="utf-8"))
    assert on_disk["parameters"]["limits"] == {"load": 3}
    assert on_disk["parameters"]["offsets"] == {"load": 1}


def test_bust_cache_is_recorded_on_the_manifest(tmp_path):
    _make_project(tmp_path)
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path), bust_cache=True)

    assert manifest["parameters"]["bust_cache"] is True
    on_disk = json.loads(
        (tmp_path / "runs" / manifest["run_id"] / "manifest.json")
        .read_text(encoding="utf-8"))
    assert on_disk["parameters"]["bust_cache"] is True


def test_an_ordinary_run_records_bust_cache_false(tmp_path):
    _make_project(tmp_path)
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path))
    assert manifest["parameters"]["bust_cache"] is False


def test_cli_bust_cache_flag_reaches_the_run(monkeypatch):
    calls: list[bool] = []

    def fake_execute(project, *, version_id=None, bindings=None, limits=None,
                     offsets=None, bust_cache=False):
        calls.append(bust_cache)
        return {"run_id": "r", "workflow_version": "v", "status": RunStatus.OK,
                "stage_records": []}

    monkeypatch.setattr(run_service, "execute", fake_execute)
    assert cli.main(["proj", "--bust-cache"]) == 0
    assert cli.main(["proj"]) == 0
    assert calls == [True, False]


def test_cli_rejects_an_unknown_flag():
    with pytest.raises(SystemExit):
        cli.main(["proj", "--nope"])


def test_per_run_override_for_unknown_stage_id_fails_loudly(tmp_path):
    _make_project(tmp_path)
    _seed_version(tmp_path)
    with pytest.raises(ValueError, match="unknown stage id"):
        execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path), limits={"nope": 3})
    with pytest.raises(ValueError, match="unknown stage id"):
        execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path), offsets={"nope": 1})


_IDENTITY_ROW_FUNCTION = "def transform(row):\n    return row\n"


def _row_mapped_project(root, rows: list[dict], code: str):
    (root / "compiled").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    pd.DataFrame(rows).to_csv(root / "data" / "items.csv", index=False)
    load = {
        "id": "load", "description": "Load items", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(root / "data" / "items.csv"), "format": "csv"}},
        "signature": {"form": "replaces", "produces": _NAME_VAL_SCHEMA["columns"]},
    }
    keep = {
        "id": "keep", "description": "Keep items", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": _NAME_VAL_SCHEMA}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": _NAME_VAL_SCHEMA["columns"]}],
        },
        "function": {"kind": "inline", "code": code},
    }
    (root / "compiled" / "01_load.json").write_text(json.dumps(load), encoding="utf-8")
    (root / "compiled" / "02_keep.json").write_text(json.dumps(keep), encoding="utf-8")


def test_offset_makes_the_trace_land_on_the_true_upstream_row(tmp_path):
    # The row driver counts from the frame it was handed; unshifted, the trace
    # would land on load's row 0.
    _row_mapped_project(tmp_path, [{"name": f"row{i}", "val": i} for i in range(5)],
                        _IDENTITY_ROW_FUNCTION)
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path),
                           offsets={"keep": 2})

    run_dir = tmp_path / "runs" / manifest["run_id"]
    assert list(pd.read_parquet(run_dir / "outputs" / "keep.parquet")["val"]) == [2, 3, 4]

    trace = trace_row(run_dir, "keep", 0)
    assert [s.stage_id for s in trace.steps] == ["keep", "load"]
    assert trace.steps[1].row_ordinal == 2
    assert trace.steps[1].row["name"] == "row2"
    assert trace.end.reached_origin is True


def test_a_limited_stage_is_not_failed_by_a_duplicate_row_it_never_reads(tmp_path):
    # Rows 0 and 3 are exact duplicates; a cap of 3 hands the stage only one of them.
    _row_mapped_project(tmp_path, [
        {"name": "a", "val": 1}, {"name": "b", "val": 2},
        {"name": "c", "val": 3}, {"name": "a", "val": 1},
    ], _IDENTITY_ROW_FUNCTION)
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path),
                           limits={"keep": 3})

    records = {r["stage_id"]: r for r in manifest["stage_records"]}
    assert records["keep"]["status"] == "ok"
    assert manifest["status"] == "ok"


def _two_stage_project(root, rows: list[dict]):
    (root / "compiled").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    pd.DataFrame(rows).to_csv(root / "data" / "items.csv", index=False)
    load = {
        "id": "load", "description": "Load items", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(root / "data" / "items.csv"), "format": "csv"}},
        "signature": {"form": "replaces", "produces": _NAME_VAL_SCHEMA["columns"]},
    }
    consume = {
        "id": "consume", "description": "Consume items", "type": "python_frame_function",
        "inputs": [{"id": "load", "schema": _NAME_VAL_SCHEMA}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": "load", "columns": _NAME_VAL_SCHEMA["columns"]}],
            "produces": _NAME_VAL_SCHEMA["columns"],
        },
        "function": {"kind": "inline",
                     "code": "def transform(df):\n    return df\n"},
    }
    (root / "compiled" / "01_load.json").write_text(
        json.dumps(load), encoding="utf-8")
    (root / "compiled" / "02_consume.json").write_text(
        json.dumps(consume), encoding="utf-8")


def test_duplicate_input_rows_fail_the_stage(tmp_path):
    # Rows 0 and 2 are identical across EVERY column — not just a `name` clash.
    _two_stage_project(tmp_path, [
        {"name": "a", "val": 1},
        {"name": "b", "val": 2},
        {"name": "a", "val": 1},
    ])
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path))

    records = {r["stage_id"]: r for r in manifest["stage_records"]}
    assert records["load"]["status"] == "ok"     # producing dupes isn't the error…
    assert records["consume"]["status"] == "error"  # …feeding them to a stage is
    msg = records["consume"]["error"]["message"]
    assert "load" in msg                          # names the offending input
    assert "[0, 2]" in msg                        # names the duplicate row numbers
    assert "row_id" in msg                        # points at the explicit-draws fix
    assert manifest["status"] == "errors"


def test_distinct_input_rows_pass(tmp_path):
    # Same `name`, distinct full rows — the documented escape hatch.
    _two_stage_project(tmp_path, [
        {"name": "a", "val": 1},
        {"name": "a", "val": 2},
    ])
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path))
    assert manifest["status"] == "ok"
    records = {r["stage_id"]: r for r in manifest["stage_records"]}
    assert records["consume"]["status"] == "ok"
    assert records["consume"]["output_row_count"] == 2


def _output_schema_violation_project(root, transform_code: str):
    (root / "compiled").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}).to_csv(
        root / "data" / "items.csv", index=False)
    load = {
        "id": "load", "description": "Load items", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(root / "data" / "items.csv"), "format": "csv"}},
        "signature": {"form": "replaces", "produces": _NAME_VAL_SCHEMA["columns"]},
    }
    shape = {
        "id": "shape", "description": "Shape items", "type": "python_frame_function",
        "inputs": [{"id": "load", "schema": _NAME_VAL_SCHEMA}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": "load", "columns": _NAME_VAL_SCHEMA["columns"]}],
            "produces": _NAME_VAL_SCHEMA["columns"],
        },
        "function": {"kind": "inline", "code": transform_code},
    }
    tail = {
        "id": "tail", "description": "Tail", "type": "python_frame_function",
        "inputs": [{"id": "shape", "schema": _NAME_VAL_SCHEMA}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": "shape", "columns": _NAME_VAL_SCHEMA["columns"]}],
            "produces": _NAME_VAL_SCHEMA["columns"],
        },
        "function": {"kind": "inline", "code": "def transform(df):\n    return df\n"},
    }
    for filename, stage in (("01_load.json", load), ("02_shape.json", shape),
                            ("03_tail.json", tail)):
        (root / "compiled" / filename).write_text(json.dumps(stage), encoding="utf-8")


def test_output_missing_a_declared_column_errors_the_stage_and_blocks_downstream(tmp_path):
    # An error-severity OUTPUT issue is a stage failure: no downstream may consume it.
    _output_schema_violation_project(
        tmp_path, "def transform(df):\n    return df[['name']]\n")
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path))

    records = {r["stage_id"]: r for r in manifest["stage_records"]}
    assert records["load"]["status"] == "ok"
    assert records["shape"]["status"] == "error"
    assert "val" in records["shape"]["error"]["message"]      # names the failing column
    # Downstream is blocked exactly as it is behind a raised exception: never
    # run, never marked ok, and no output left behind for a resume to reuse.
    assert records["tail"]["status"] == "pending"
    assert records["tail"].get("output_path") is None
    assert not (tmp_path / "runs" / manifest["run_id"] / "outputs" / "tail.parquet").exists()
    assert manifest["status"] == "errors"


def test_warning_only_output_report_does_not_error_the_stage(tmp_path):
    # An undeclared extra column is warning-severity: every declared column is there.
    _output_schema_violation_project(
        tmp_path, "def transform(df):\n    df['extra'] = 1\n    return df\n")
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path))

    records = {r["stage_id"]: r for r in manifest["stage_records"]}
    assert records["shape"]["status"] != "error"
    assert records["shape"]["error"] is None
    issues = records["shape"]["output_validation_report"]["issues"]
    assert [i["severity"] for i in issues] == ["warning"]   # warning-only, and reported
    assert records["tail"]["status"] == "ok"                # not blocked
    assert manifest["status"] != "errors"


def test_output_validation_error_other_than_a_missing_column_also_errors_the_stage(tmp_path):
    # The rule is on severity, not on one issue kind.
    (tmp_path / "compiled").mkdir(parents=True)
    (tmp_path / "data").mkdir(parents=True)
    pd.DataFrame({"name": ["a"], "val": [1]}).to_csv(
        tmp_path / "data" / "items.csv", index=False)
    load = {
        "id": "load", "description": "Load items", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(tmp_path / "data" / "items.csv"), "format": "csv"}},
        "signature": {"form": "replaces", "produces": _NAME_VAL_SCHEMA["columns"]},
    }
    blank = {
        "id": "blank", "description": "Blank the value", "type": "python_frame_function",
        "inputs": [{"id": "load", "schema": _NAME_VAL_SCHEMA}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": "load", "columns": _NAME_VAL_SCHEMA["columns"]}],
            "produces": [
                {"name": "name", "type": "str", "nullable": True},
                {"name": "val", "type": "int", "nullable": False},
            ],
        },
        "function": {"kind": "inline",
                     "code": "def transform(df):\n    df['val'] = None\n    return df\n"},
    }
    (tmp_path / "compiled" / "01_load.json").write_text(json.dumps(load), encoding="utf-8")
    (tmp_path / "compiled" / "02_blank.json").write_text(json.dumps(blank), encoding="utf-8")
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path))

    record = {r["stage_id"]: r for r in manifest["stage_records"]}["blank"]
    assert record["status"] == "error"
    assert record["error"]["type"] == "OutputSchemaViolation"
    assert "val" in record["error"]["message"]
    assert manifest["status"] == "errors"


def test_value_outside_a_declared_enum_errors_the_stage_and_blocks_downstream(tmp_path):
    (tmp_path / "compiled").mkdir(parents=True)
    (tmp_path / "data").mkdir(parents=True)
    pd.DataFrame({"name": ["a"], "val": [1]}).to_csv(
        tmp_path / "data" / "items.csv", index=False)
    labelled_schema = {"columns": [
        {"name": "name", "type": "str", "nullable": True},
        {"name": "status", "type": "str", "enum": ["open", "closed"], "nullable": True},
    ]}
    load = {
        "id": "load", "description": "Load items", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(tmp_path / "data" / "items.csv"), "format": "csv"}},
        "signature": {"form": "replaces", "produces": _NAME_VAL_SCHEMA["columns"]},
    }
    label = {
        "id": "label", "description": "Label items", "type": "python_frame_function",
        "inputs": [{"id": "load", "schema": _NAME_VAL_SCHEMA}],
        "signature": {"form": "replaces", "produces": labelled_schema["columns"]},
        "function": {"kind": "inline",
                     "code": "def transform(df):\n"
                             "    return df.assign(status='pending')[['name', 'status']]\n"},
    }
    tail = {
        "id": "tail", "description": "Tail", "type": "python_frame_function",
        "inputs": [{"id": "label", "schema": labelled_schema}],
        "signature": {"form": "replaces", "produces": labelled_schema["columns"]},
        "function": {"kind": "inline", "code": "def transform(df):\n    return df\n"},
    }
    for filename, stage in (("01_load.json", load), ("02_label.json", label),
                            ("03_tail.json", tail)):
        (tmp_path / "compiled" / filename).write_text(json.dumps(stage), encoding="utf-8")
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path))

    records = {r["stage_id"]: r for r in manifest["stage_records"]}
    assert records["label"]["status"] == "error"
    assert records["label"]["error"]["type"] == "OutputSchemaViolation"
    assert "status" in records["label"]["error"]["message"]
    assert "'pending'" in records["label"]["error"]["message"]
    assert records["tail"]["status"] == "pending"
    assert manifest["status"] == "errors"


def _llm_transform_project(root):
    (root / "compiled").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    pd.DataFrame({"id": ["r1"], "text": ["hi"]}).to_csv(
        root / "data" / "items.csv", index=False)
    load = {
        "id": "load", "description": "Load items", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(root / "data" / "items.csv"), "format": "csv"}},
        "signature": {"form": "replaces", "produces": _ID_TEXT_SCHEMA["columns"]},
    }
    score = {
        "id": "score", "description": "Score items", "type": "llm_transform",
        "inputs": [{"id": "load", "schema": _ID_TEXT_SCHEMA}],
        "signature": {
            "form": "extends",
            "reads": [
                {
                    "input": "load",
                    "columns": [{"name": "text", "type": "str", "nullable": True}],
                },
            ],
            "adds": [{"name": "score", "type": "int", "nullable": False}],
        },
        "llm": {"prompt_template": "Rate: {text}"},
    }
    (root / "compiled" / "01_load.json").write_text(json.dumps(load), encoding="utf-8")
    (root / "compiled" / "02_score.json").write_text(json.dumps(score), encoding="utf-8")


def test_llm_generation_failure_surfaces_as_error_status_not_raised(tmp_path, monkeypatch):
    # Without raising — the stage still completes and keeps its (partial) output.
    def boom(stage_id, llm_config, row, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(lt, "call_llm", boom)
    _llm_transform_project(tmp_path)
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path))

    records = {r["stage_id"]: r for r in manifest["stage_records"]}
    rec = records["score"]
    assert rec["status"] == "error"
    assert rec["output_row_count"] == 1                         # stage completed, output kept
    issues = rec["output_validation_report"]["issues"]
    assert any("generation failed" in i["message"] and "boom" in i["message"]
               for i in issues)
    assert rec["error"]["type"] == "RowGenerationError"
    assert "boom" in rec["error"]["message"]         # the real reason, not "unknown error"
    assert rec["error"]["traceback"] is None         # distinguishes it from a raised exception
    assert manifest["status"] == "errors"


def test_run_subset_surfaces_the_real_row_failure_message(tmp_path, monkeypatch):
    # run_subset backs eval/preview runs (app/evals/runner.py).
    def boom(stage_id, llm_config, row, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(lt, "call_llm", boom)
    # Path-free connector: `load`'s output is injected below, so no file exists
    # or is read — declaring one would be a fabricated fixture value.
    load = parse_stage({
        "id": "load", "description": "Load items", "type": "input_data",
        "connector": {"kind": "file"},
        "signature": {"form": "replaces", "produces": _ID_TEXT_SCHEMA["columns"]},
    })
    score = parse_stage({
        "id": "score", "description": "Score items", "type": "llm_transform",
        "inputs": [{"id": "load", "schema": _ID_TEXT_SCHEMA}],
        "signature": {
            "form": "extends",
            "reads": [
                {
                    "input": "load",
                    "columns": [{"name": "text", "type": "str", "nullable": True}],
                },
            ],
            "adds": [{"name": "score", "type": "int", "nullable": False}],
        },
        "llm": {"prompt_template": "Rate: {text}"},
    })
    workflow = Workflow(stages=[load, score])
    injected_outputs = {"load": pd.DataFrame({"id": ["r1"], "text": ["hi"]})}

    with pytest.raises(SubsetRunError) as exc_info:
        run_subset(
            workflow, injected_outputs=injected_outputs, stage_ids=["score"],
            run_dir=tmp_path / "runs" / "subset1", repo_root=tmp_path,
        )

    message = str(exc_info.value)
    assert "failed generation" in message and "boom" in message
    assert "unknown error" not in message


def test_run_subset_preserves_partial_work_in_the_manifest_on_a_mid_frontier_error(tmp_path):
    # run_subset saves as it goes, so the partial work is on disk at the error.
    load = parse_stage({
        "id": "load", "description": "Load items", "type": "input_data",
        "connector": {"kind": "file"},
        "signature": {"form": "replaces", "produces": _ID_TEXT_SCHEMA["columns"]},
    })
    clean = parse_stage({
        "id": "clean", "description": "Clean rows", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": {
            "columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "text", "type": "str", "nullable": True}]}}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": _ID_TEXT_SCHEMA["columns"]}],
        },
        "function": {"kind": "inline", "code": "def transform(row): return row"},
    })
    boom = parse_stage({
        "id": "score", "description": "Score rows", "type": "python_row_function",
        "inputs": [{"id": "clean", "schema": {
            "columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "text", "type": "str", "nullable": True}]}}],
        "signature": {"form": "extends",
                      "adds": [{"name": "score", "type": "int", "nullable": True}]},
        "function": {"kind": "inline",
                     "code": "def transform(row):\n    raise ValueError('kaboom')"},
    })
    workflow = Workflow(stages=[load, clean, boom])
    injected = {"load": pd.DataFrame({"id": ["r1"], "text": ["hi"]})}
    run_dir = tmp_path / "runs" / "partial"

    with pytest.raises(SubsetRunError):
        run_subset(
            workflow, injected_outputs=injected, stage_ids=["clean", "score"],
            run_dir=run_dir, repo_root=tmp_path)

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    records = {r["stage_id"]: r for r in manifest["stage_records"]}
    assert records["clean"]["status"] == "ok"          # completed upstream preserved
    assert records["score"]["status"] == "error"       # failing stage's error recorded
    assert records["score"]["error"] is not None
    assert "kaboom" in records["score"]["error"]["message"]
    assert manifest["status"] == "errors"
    # Identity not supplied to this subset run — recorded honestly, not fabricated.
    assert manifest["project"] is None
    assert manifest["workflow_version"] is None


def test_raise_if_run_failed_lists_halted_stages_as_readable_text():
    manifest = RunManifest(
        run_id="r", started_at="t", project=None, workflow_version=None,
        limit_overrides={}, offset_overrides={}, run_bindings={}, input_bindings={},
        human_review_queue_stats={}, dropped_columns={}, status="awaiting_review",
        stage_records=[], halted_at=["review_a", "review_b"],
    )

    with pytest.raises(SubsetRunError) as exc_info:
        _raise_if_run_failed(manifest)

    message = str(exc_info.value)
    assert "review_a, review_b" in message
    assert "[" not in message and "]" not in message


def test_run_without_a_version_fails_loudly(tmp_path):
    _make_project(tmp_path)  # valid working copy, but no version created
    with pytest.raises(NoVersionToRunError):
        execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path))
    assert not (tmp_path / "runs").exists()
    assert list_versions(tmp_path) == []


def test_the_newest_version_runs_even_when_an_older_one_is_the_published_one(tmp_path):
    _make_project(tmp_path)
    published_id = _seed_version(tmp_path)
    versioning.publish_version(tmp_path, published_id, reviewer="human")

    time.sleep(1)  # version ids are second-resolution
    newer = save_working_copy_as_version(
        tmp_path, message="unpublished newer", reviewer="test").version_id

    manifest = execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path))
    assert manifest["workflow_version"] == newer
    assert manifest["status"] == "ok"


def test_run_with_no_published_version_succeeds(tmp_path):
    _make_project(tmp_path)
    vid = save_working_copy_as_version(tmp_path, message="unpublished", reviewer="test").version_id

    manifest = execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path))
    assert manifest["workflow_version"] == vid
    assert manifest["status"] == "ok"


def test_run_with_explicit_unpublished_id_succeeds(tmp_path):
    _make_project(tmp_path)
    unpublished_id = save_working_copy_as_version(tmp_path, message="unpublished", reviewer="test").version_id

    manifest = execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path, unpublished_id))
    assert manifest["workflow_version"] == unpublished_id
    assert manifest["status"] == "ok"


def test_create_version_rejects_invalid_working_copy(tmp_path):
    (tmp_path / "compiled").mkdir(parents=True)
    bad = {"id": "load", "description": "Load", "type": "input_data",
           "connector": {"kind": "file",
                         "params": {"path": "data/items.csv", "format": "csv"}},  # relative path
           "signature": {"form": "replaces", "produces": _NAME_VAL_SCHEMA["columns"]}}
    (tmp_path / "compiled" / "01_load.json").write_text(
        json.dumps(bad), encoding="utf-8")

    with pytest.raises(WorkflowLoadError) as exc:
        save_working_copy_as_version(tmp_path, message="x", reviewer="test")
    assert any("params.path" in i for i in exc.value.issues)
    assert list_versions(tmp_path) == []  # snapshotted nothing


def test_invalid_workflow_never_becomes_a_version_and_run_never_pins_stale(tmp_path):
    # Invalid working copy: file connector params.path is relative, not absolute.
    (tmp_path / "compiled").mkdir(parents=True)
    bad = {"id": "load", "description": "Load", "type": "input_data",
           "connector": {"kind": "file",
                         "params": {"path": "data/items.csv", "format": "csv"}},
           "signature": {"form": "replaces", "produces": _NAME_VAL_SCHEMA["columns"]}}
    (tmp_path / "compiled" / "01_load.json").write_text(
        json.dumps(bad), encoding="utf-8")

    # You cannot make a version from it, and it writes nothing.
    with pytest.raises(WorkflowLoadError):
        save_working_copy_as_version(tmp_path, message="x", reviewer="test")
    assert list_versions(tmp_path) == []

    # A run refuses (no version) and does NOT auto-create one — nothing on disk.
    with pytest.raises(NoVersionToRunError):
        execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path))
    assert list_versions(tmp_path) == []
    assert not (tmp_path / "runs").exists()

    # Fix the working copy. A run STILL refuses until a version is created
    # explicitly — it never silently pins to a stale snapshot (there is none).
    (tmp_path / "data").mkdir(parents=True)
    pd.DataFrame({"name": ["a"], "val": [1]}).to_csv(
        tmp_path / "data" / "items.csv", index=False)
    good = {"id": "load", "description": "Load", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": str(tmp_path / "data" / "items.csv"), "format": "csv"}},
            "signature": {"form": "replaces", "produces": _NAME_VAL_SCHEMA["columns"]}}
    (tmp_path / "compiled" / "01_load.json").write_text(
        json.dumps(good), encoding="utf-8")
    with pytest.raises(NoVersionToRunError):
        execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path))

    # Explicit creation, then the run succeeds against that version.
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path))
    assert manifest["status"] == "ok"


def test_resume_reapplies_run_bindings_for_a_pending_input_stage(tmp_path):
    """Hand-builds the post-halt manifest: a real halt through human_review_queue is
    disproportionate."""
    (tmp_path / "compiled").mkdir(parents=True)
    stage = {"id": "load", "description": "Load items", "type": "input_data",
              "connector": {"kind": "file", "params": {}},  # no workflow-authored path
              "signature": {"form": "replaces", "produces": _NAME_VAL_SCHEMA["columns"]}}
    (tmp_path / "compiled" / "01_load.json").write_text(
        json.dumps(stage), encoding="utf-8")
    version_id = _seed_version(tmp_path)

    bound_csv = tmp_path / "bound.csv"
    pd.DataFrame({"name": ["bound-row"], "val": [42]}).to_csv(bound_csv, index=False)

    run_id = "20260101T000000"
    run_dir = tmp_path / "runs" / run_id
    (run_dir / "outputs").mkdir(parents=True)
    manifest = {
        "run_id": run_id, "started_at": run_id, "project": tmp_path.name,
        "workflow_version": version_id,
        "status": "awaiting_review",
        "run_bindings": {"load": {"path": str(bound_csv)}},
        "input_bindings": {
            "load": {"path": str(bound_csv), "source": "run",
                     "sha256": "unused-in-this-test", "bytes": bound_csv.stat().st_size},
        },
        "human_review_queue_stats": {},
        "stage_records": [{"stage_id": "load", "type": "input_data", "description": "Load items",
                    "status": "pending", "input_validation_report": [],
                    "output_validation_report": None,
                    "elapsed_ms": 0, "output_row_count": 0, "error": None,
                    "started_at": None, "finished_at": None}],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = resume_run(tmp_path, run_id, tmp_path, *resumed_stages(tmp_path, run_id))

    [rec] = result["stage_records"]
    assert rec["status"] == "ok", rec.get("error")
    out = pd.read_parquet(run_dir / "outputs" / "load.parquet")
    assert list(out["name"]) == ["bound-row"]


def test_the_documented_cli_runs_a_project_with_nothing_configured(
    tmp_path, monkeypatch, projects_root
):
    """Drops both process-wide stores: no server lifespan wires storage for the CLI."""
    from app.core import frames as frames_module
    from app.core import persistence as persistence_module
    from app.core.persistence import SqliteKvStore, configure_store

    db_path = tmp_path / "db" / "app.db"
    db_path.parent.mkdir(parents=True)
    monkeypatch.setenv("CARBONPAPER_DB_PATH", str(db_path))
    monkeypatch.setenv("CARBONPAPER_FRAMES_ROOT", str(tmp_path / "frames"))

    project_dir = projects_root / "project"
    configure_store(SqliteKvStore(str(db_path)))
    _make_project(project_dir)
    _add_frame_stage(project_dir)
    _seed_version(project_dir)

    monkeypatch.setattr(persistence_module, "_store", None)
    monkeypatch.setattr(frames_module, "_frame_store", None)

    assert cli.main(["project"]) == 0
    assert persistence_module.is_store_configured()
    assert frames_module.is_frame_store_configured()
    assert list((project_dir / "runs").iterdir())


_FRAME_STAGE_CODE = "def transform(df):\n    return df.assign(double=df['val'] * 2)\n"


def _add_frame_stage(root):
    (root / "compiled" / "02_totals.json").write_text(json.dumps({
        "id": "totals", "description": "Totals", "type": "python_frame_function",
        "inputs": [{"id": "load", "schema": _NAME_VAL_SCHEMA}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": "load", "columns": _NAME_VAL_SCHEMA["columns"]}],
            "produces": [
                {"name": "name", "type": "str", "nullable": True},
                {"name": "val", "type": "int", "nullable": True},
                {"name": "double", "type": "int", "nullable": True},
            ],
        },
        "function": {"kind": "inline", "code": _FRAME_STAGE_CODE},
    }), encoding="utf-8")


def test_a_frame_stage_succeeds_with_no_frame_store_configured(tmp_path, monkeypatch):
    from app.core import frames as frames_module

    _make_project(tmp_path)
    _add_frame_stage(tmp_path)
    _seed_version(tmp_path)
    monkeypatch.setattr(frames_module, "_frame_store", None)

    manifest = execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path))

    assert manifest["status"] == "ok"
    record = next(r for r in manifest["stage_records"] if r["stage_id"] == "totals")
    assert record["status"] == "ok"
    assert record["output_row_count"] == 2
    assert any("no frame store" in note for note in record["notes"])

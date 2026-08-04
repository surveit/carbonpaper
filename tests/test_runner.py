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
from app.runtime.manifest import RunManifest
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
    """Create the initial version a run targets, and PUBLISH it. Runs no longer
    create versions, so a test that builds a working copy must snapshot it into
    a version before running against it; runs are also gated on published, so
    the seed must be published for a run against it to succeed."""
    vid = save_working_copy_as_version(root, message="test seed", reviewer="test").version_id
    versioning.publish_version(root, vid, reviewer="human")
    return vid


def _make_project(root):
    (root / "compiled").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    pd.DataFrame({"name": [f"row{i}" for i in range(5)], "val": list(range(5))}) \
        .to_csv(root / "data" / "items.csv", index=False)
    stage = {
        "id": "load", "name": "Load items", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(root / "data" / "items.csv"), "format": "csv"}},
        "output_schema": _NAME_VAL_SCHEMA,
        "limit": 2,
    }
    (root / "compiled" / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")


def test_limit_on_a_source_stage_caps_the_rows_it_loads(tmp_path):
    # input_data is the one stage type with no input frames, so the frame it
    # just loaded is the runtime's only handle on its rows: the cap lands there.
    # A limit that silently did nothing on a source would be the worst outcome.
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
    # 5 rows, static `limit: 2` in the stage spec. The per-run cap wins over
    # the static one, and the offset skips rows BEFORE the cap is applied:
    # offset 1 skips row 0, then limit 3 reads rows 1-3.
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
    assert manifest["limit_overrides"] == {"load": 3}
    assert manifest["offset_overrides"] == {"load": 1}
    notes = rec.get("notes", [])
    assert any(n.startswith("offset=1") for n in notes)
    assert any(n.startswith("limit=3") for n in notes)

    on_disk = json.loads(
        (tmp_path / "runs" / manifest["run_id"] / "manifest.json")
        .read_text(encoding="utf-8"))
    assert on_disk["limit_overrides"] == {"load": 3}
    assert on_disk["offset_overrides"] == {"load": 1}


def test_bust_cache_is_recorded_on_the_manifest(tmp_path):
    """A recompute-everything run records the flag as run provenance, so a
    reader (and the resume) knows this run refused every cache read."""
    _make_project(tmp_path)
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path), bust_cache=True)

    assert manifest["bust_cache"] is True
    on_disk = json.loads(
        (tmp_path / "runs" / manifest["run_id"] / "manifest.json")
        .read_text(encoding="utf-8"))
    assert on_disk["bust_cache"] is True


def test_an_ordinary_run_records_bust_cache_false(tmp_path):
    _make_project(tmp_path)
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path))
    assert manifest["bust_cache"] is False


def test_cli_bust_cache_flag_reaches_the_run(monkeypatch):
    """--bust-cache threads into the run; without it the run is not busted."""
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
    """input_data loading `rows` from CSV, feeding a python_row_function running `code`."""
    (root / "compiled").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    pd.DataFrame(rows).to_csv(root / "data" / "items.csv", index=False)
    load = {
        "id": "load", "name": "Load items", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(root / "data" / "items.csv"), "format": "csv"}},
        "output_schema": _NAME_VAL_SCHEMA,
    }
    keep = {
        "id": "keep", "name": "Keep items", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": _NAME_VAL_SCHEMA}],
        "output_schema": _NAME_VAL_SCHEMA,
        "function": {"kind": "inline", "code": code},
    }
    (root / "compiled" / "01_load.json").write_text(json.dumps(load), encoding="utf-8")
    (root / "compiled" / "02_keep.json").write_text(json.dumps(keep), encoding="utf-8")


def test_offset_makes_the_trace_land_on_the_true_upstream_row(tmp_path):
    # The row driver counts from the frame it was HANDED, which an offset starts
    # two rows in. Unshifted, that lineage would send the trace to load's row 0.
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
    # Rows 0 and 3 are exact duplicates, which fails any stage fed both. Under a
    # cap of 3 the stage is handed neither pair member twice, so it runs clean:
    # a dry run must not be failed by a row outside the window it asked for.
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
    """input_data loading `rows` from CSV, feeding an identity
    python_frame_function. Exercises the runner's per-stage input checks."""
    (root / "compiled").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    pd.DataFrame(rows).to_csv(root / "data" / "items.csv", index=False)
    load = {
        "id": "load", "name": "Load items", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(root / "data" / "items.csv"), "format": "csv"}},
        "output_schema": _NAME_VAL_SCHEMA,
    }
    consume = {
        "id": "consume", "name": "Consume items", "type": "python_frame_function",
        "inputs": [{"id": "load", "schema": _NAME_VAL_SCHEMA}],
        "output_schema": _NAME_VAL_SCHEMA,
        "function": {"kind": "inline",
                     "code": "def transform(df):\n    return df\n"},
    }
    (root / "compiled" / "01_load.json").write_text(
        json.dumps(load), encoding="utf-8")
    (root / "compiled" / "02_consume.json").write_text(
        json.dumps(consume), encoding="utf-8")


def test_duplicate_input_rows_fail_the_stage(tmp_path):
    # Rows 0 and 2 are identical across EVERY column. That the `name` values
    # collide is not the point — full-content duplication is.
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
    # Same values in `name` but distinct full rows — an explicit
    # distinguishing column is exactly the documented escape hatch.
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
    """load → shape (a frame function running `transform_code`) → tail. `shape`
    declares the (name, val) schema; what its code actually returns is the
    variable under test, and `tail` exists to show what the run does downstream
    of it."""
    (root / "compiled").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}).to_csv(
        root / "data" / "items.csv", index=False)
    load = {
        "id": "load", "name": "Load items", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(root / "data" / "items.csv"), "format": "csv"}},
        "output_schema": _NAME_VAL_SCHEMA,
    }
    shape = {
        "id": "shape", "name": "Shape items", "type": "python_frame_function",
        "inputs": [{"id": "load", "schema": _NAME_VAL_SCHEMA}],
        "output_schema": _NAME_VAL_SCHEMA,
        "function": {"kind": "inline", "code": transform_code},
    }
    tail = {
        "id": "tail", "name": "Tail", "type": "python_frame_function",
        "inputs": [{"id": "shape", "schema": _NAME_VAL_SCHEMA}],
        "output_schema": _NAME_VAL_SCHEMA,
        "function": {"kind": "inline", "code": "def transform(df):\n    return df\n"},
    }
    for filename, stage in (("01_load.json", load), ("02_shape.json", shape),
                            ("03_tail.json", tail)):
        (root / "compiled" / filename).write_text(json.dumps(stage), encoding="utf-8")


def test_output_missing_a_declared_column_errors_the_stage_and_blocks_downstream(tmp_path):
    # An error-severity OUTPUT issue is a stage failure, not a warning: the
    # frame does not satisfy the declared schema, so no downstream stage may
    # consume it. Same fork-block a raised handler exception gets.
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
    # An undeclared extra column is warning-severity. It must not be swept into
    # the error rule — every declared column is there, so downstream may consume
    # the frame and the stage carries no error record.
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
    # The rule is on severity, not on one issue kind: a null in a column
    # declared non-nullable is error-severity and fails the stage the same way.
    (tmp_path / "compiled").mkdir(parents=True)
    (tmp_path / "data").mkdir(parents=True)
    pd.DataFrame({"name": ["a"], "val": [1]}).to_csv(
        tmp_path / "data" / "items.csv", index=False)
    load = {
        "id": "load", "name": "Load items", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(tmp_path / "data" / "items.csv"), "format": "csv"}},
        "output_schema": _NAME_VAL_SCHEMA,
    }
    blank = {
        "id": "blank", "name": "Blank the value", "type": "python_frame_function",
        "inputs": [{"id": "load", "schema": _NAME_VAL_SCHEMA}],
        "output_schema": {"columns": [{"name": "name", "type": "str", "nullable": True},
                                      {"name": "val", "type": "int", "nullable": False}]},
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
        "id": "load", "name": "Load items", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(tmp_path / "data" / "items.csv"), "format": "csv"}},
        "output_schema": _NAME_VAL_SCHEMA,
    }
    label = {
        "id": "label", "name": "Label items", "type": "python_frame_function",
        "inputs": [{"id": "load", "schema": _NAME_VAL_SCHEMA}],
        "output_schema": labelled_schema,
        "function": {"kind": "inline",
                     "code": "def transform(df):\n"
                             "    return df.assign(status='pending')[['name', 'status']]\n"},
    }
    tail = {
        "id": "tail", "name": "Tail", "type": "python_frame_function",
        "inputs": [{"id": "label", "schema": labelled_schema}],
        "output_schema": labelled_schema,
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
    """input_data loading one row, feeding an llm_transform. Exercises the
    runner's row-error surfacing when a row's generation fails."""
    (root / "compiled").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    pd.DataFrame({"id": ["r1"], "text": ["hi"]}).to_csv(
        root / "data" / "items.csv", index=False)
    load = {
        "id": "load", "name": "Load items", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(root / "data" / "items.csv"), "format": "csv"}},
        "output_schema": _ID_TEXT_SCHEMA,
    }
    score = {
        "id": "score", "name": "Score items", "type": "llm_transform",
        "inputs": [{"id": "load", "schema": _ID_TEXT_SCHEMA}],
        "output_schema": {
            "columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "text", "type": "str", "nullable": True},
                        {"name": "score", "type": "int", "nullable": False}]},
        "llm": {"prompt_template": "Rate: {text}"},
    }
    (root / "compiled" / "01_load.json").write_text(json.dumps(load), encoding="utf-8")
    (root / "compiled" / "02_score.json").write_text(json.dumps(score), encoding="utf-8")


def test_llm_generation_failure_surfaces_as_error_status_not_raised(tmp_path, monkeypatch):
    # A row's generation failure must show up as an error-severity output issue,
    # flip the stage to status=error, AND carry a populated error record naming
    # the real cause (so _raise_if_run_failed / eval subset runs don't report
    # "unknown error") — WITHOUT raising, so the stage still completes and keeps
    # its (partial) output.
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
    # run_subset backs eval/preview runs (app/evals/runner.py). A row failure
    # must raise SubsetRunError naming the real cause via record["error"] —
    # not "unknown error" — because _raise_if_run_failed reads that field.
    def boom(stage_id, llm_config, row, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(lt, "call_llm", boom)
    # Path-free connector: `load`'s output is injected below, so no file exists
    # or is read — declaring one would be a fabricated fixture value.
    load = parse_stage({
        "id": "load", "name": "Load items", "type": "input_data",
        "connector": {"kind": "file"},
        "output_schema": _ID_TEXT_SCHEMA,
    })
    score = parse_stage({
        "id": "score", "name": "Score items", "type": "llm_transform",
        "inputs": [{"id": "load", "schema": _ID_TEXT_SCHEMA}],
        "output_schema": {
            "columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "text", "type": "str", "nullable": True},
                        {"name": "score", "type": "int", "nullable": False}]},
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
    # run_subset owns a live manifest: when a mid-frontier stage errors, the
    # manifest on disk at that moment must already show the completed upstream
    # stage as ok and the failing stage's error — partial work is preserved for a
    # caller (workflow test / eval) to read back, not lost to a save-at-the-end.
    load = parse_stage({
        "id": "load", "name": "Load items", "type": "input_data",
        "connector": {"kind": "file"},
        "output_schema": _ID_TEXT_SCHEMA,
    })
    clean = parse_stage({
        "id": "clean", "name": "Clean rows", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": {
            "columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "text", "type": "str", "nullable": True}]}}],
        "output_schema": {
            "columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "text", "type": "str", "nullable": True}]},
        "function": {"kind": "inline", "code": "def transform(row): return row"},
    })
    boom = parse_stage({
        "id": "score", "name": "Score rows", "type": "python_row_function",
        "inputs": [{"id": "clean", "schema": {
            "columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "text", "type": "str", "nullable": True}]}}],
        "output_schema": {
            "columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "score", "type": "int", "nullable": True}]},
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
    """`halted_at` is a list of stage ids (see app/runtime/runner.py's
    _execute_stages). _raise_if_run_failed's message must read them out
    comma-joined, not as Python's list repr (`['review_a', 'review_b']`)."""
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
    """A run targets an existing version and never creates one: a valid but
    unversioned working copy raises NoVersionToRunError and leaves nothing on
    disk — no run dir, no fabricated version."""
    _make_project(tmp_path)  # valid working copy, but no version created
    with pytest.raises(NoVersionToRunError):
        execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path))
    assert not (tmp_path / "runs").exists()
    assert list_versions(tmp_path) == []


def test_unpublished_latest_is_skipped_for_an_older_published_version(tmp_path):
    """A run given no explicit version_id pins to the newest PUBLISHED version,
    not merely the newest version — an unpublished draft snapshot never leaks
    into a run just because it is more recent."""
    _make_project(tmp_path)
    published_id = _seed_version(tmp_path)  # published

    time.sleep(1)  # version ids are second-resolution
    save_working_copy_as_version(tmp_path, message="unpublished newer", reviewer="test")

    manifest = execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path))
    assert manifest["workflow_version"] == published_id
    assert manifest["status"] == "ok"


def test_run_with_no_published_version_fails_loudly(tmp_path):
    """A version exists but nothing is published yet: a run still refuses,
    just like the no-version-at-all case, rather than silently running an
    unreviewed snapshot."""
    _make_project(tmp_path)
    save_working_copy_as_version(tmp_path, message="unpublished", reviewer="test")

    with pytest.raises(NoVersionToRunError):
        execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path))
    assert not (tmp_path / "runs").exists()


def test_run_with_explicit_unpublished_id_fails_loudly(tmp_path):
    """An explicit version_id naming a real but unpublished version is refused
    the same way — a run pins to a published version, never to a draft."""
    _make_project(tmp_path)
    unpublished_id = save_working_copy_as_version(tmp_path, message="unpublished", reviewer="test").version_id

    with pytest.raises(NoVersionToRunError):
        execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path), version_id=unpublished_id)
    assert not (tmp_path / "runs").exists()


def test_create_version_rejects_invalid_working_copy(tmp_path):
    """save_working_copy_as_version strict-loads before it snapshots: an invalid
    working copy raises WorkflowLoadError and writes NOTHING, so no invalid
    workflow can be immortalised as a version."""
    (tmp_path / "compiled").mkdir(parents=True)
    bad = {"id": "load", "name": "Load", "type": "input_data",
           "connector": {"kind": "file",
                         "params": {"path": "data/items.csv", "format": "csv"}},  # relative path
           "output_schema": _NAME_VAL_SCHEMA}
    (tmp_path / "compiled" / "01_load.json").write_text(
        json.dumps(bad), encoding="utf-8")

    with pytest.raises(WorkflowLoadError) as exc:
        save_working_copy_as_version(tmp_path, message="x", reviewer="test")
    assert any("params.path" in i for i in exc.value.issues)
    assert list_versions(tmp_path) == []  # snapshotted nothing


def test_invalid_workflow_never_becomes_a_version_and_run_never_pins_stale(tmp_path):
    """Regression for the version-lifecycle bug: a run used to snapshot the
    working copy as a version BEFORE validating it, so an invalid workflow got
    immortalised as 'the latest' and every later default run reloaded that
    poisoned snapshot and failed with a stale error. Now runs never create
    versions and save_working_copy_as_version validates first, so the bug is
    impossible."""
    # Invalid working copy: file connector params.path is relative, not absolute.
    (tmp_path / "compiled").mkdir(parents=True)
    bad = {"id": "load", "name": "Load", "type": "input_data",
           "connector": {"kind": "file",
                         "params": {"path": "data/items.csv", "format": "csv"}},
           "output_schema": _NAME_VAL_SCHEMA}
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
    good = {"id": "load", "name": "Load", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": str(tmp_path / "data" / "items.csv"), "format": "csv"}},
            "output_schema": _NAME_VAL_SCHEMA}
    (tmp_path / "compiled" / "01_load.json").write_text(
        json.dumps(good), encoding="utf-8")
    with pytest.raises(NoVersionToRunError):
        execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path))

    # Explicit creation, then the run succeeds against that version.
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path))
    assert manifest["status"] == "ok"


def test_resume_reapplies_run_bindings_for_a_pending_input_stage(tmp_path):
    """Regression: an input stage that had NOT executed before a halt must
    resume using its manifest-recorded RUN binding, not whatever path (or
    absence of one) the workflow itself authors — otherwise the manifest's
    `source: "run"` provenance record would be a lie, and a workflow-authored
    path-free input stage would KeyError on resume instead of reading the
    bound file.

    Constructing a genuine halt-then-resume through human_review_queue is
    disproportionate scaffolding for this fix, so this exercises resume_run's
    actual contract directly: a hand-built manifest (as prepare_run + a halt
    would have produced) naming a pending input stage with a `source: "run"`
    binding, and a workflow that authors NO path for it."""
    (tmp_path / "compiled").mkdir(parents=True)
    stage = {"id": "load", "name": "Load items", "type": "input_data",
              "connector": {"kind": "file", "params": {}},  # no workflow-authored path
              "output_schema": _NAME_VAL_SCHEMA}
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
        "stage_records": [{"stage_id": "load", "type": "input_data", "name": "Load items",
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
    """`python -m app.cli <project>` is a standalone process: no
    server lifespan wired storage for it, so its own entry point must. Seeds a
    version through an on-disk store, then drops BOTH process-wide stores to
    simulate the fresh process the CLI actually runs in."""
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
    """A python_frame_function downstream of `load` — the shape the frame cache
    intercepts, so a run of this project exercises the frame store."""
    (root / "compiled" / "02_totals.json").write_text(json.dumps({
        "id": "totals", "name": "Totals", "type": "python_frame_function",
        "inputs": [{"id": "load", "schema": _NAME_VAL_SCHEMA}],
        "output_schema": {"columns": [*_NAME_VAL_SCHEMA["columns"],
                                      {"name": "double", "type": "int", "nullable": True}]},
        "function": {"kind": "inline", "code": _FRAME_STAGE_CODE},
    }), encoding="utf-8")


def test_a_frame_stage_succeeds_with_no_frame_store_configured(tmp_path, monkeypatch):
    """The dogfooded regression: a process with a document store but no frame
    store must still run a frame stage — a cache miss is never a stage error."""
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

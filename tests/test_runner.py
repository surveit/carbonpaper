"""Integration: the runner's row slicing (static `limit:` + per-run
--limit/--offset overrides) with manifest persistence, the duplicate-input-row
check at every stage boundary, and the version-lifecycle invariant that a run
targets an existing version and never creates one.

Builds small file-connector projects in a tmp dir, snapshots them into a
version, runs them, and checks that `limit:` truncated the output, that per-run
--limit/--offset slice the output and are recorded as run provenance (not
silent), that manifest.json landed on disk, and that a stage fed exact duplicate
full-content rows fails loudly naming them. Also checks that an unversioned or
invalid working copy is refused loudly, writing nothing.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app.errors import NoVersionToRunError
from app.runtime.runner import execute_run
from app.services.loader import WorkflowLoadError
from app.services.versioning import create_version


def _seed_version(root):
    """Create the initial version a run targets. Runs no longer create versions,
    so a test that builds a working copy must snapshot it into a version before
    running against it."""
    return create_version(root, message="test seed", reviewer="test")["id"]


def _make_project(root):
    (root / "compiled").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    pd.DataFrame({"name": [f"row{i}" for i in range(5)], "val": list(range(5))}) \
        .to_csv(root / "data" / "items.csv", index=False)
    stage = {
        "id": "load", "name": "Load items", "type": "input_data",
        "connector": {"kind": "file", "params": {"path": "data/items.csv", "format": "csv"}},
        "limit": 2,
    }
    (root / "compiled" / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")


def test_limit_truncates_and_is_recorded(tmp_path):
    _make_project(tmp_path)
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, repo_root=tmp_path)

    assert manifest["status"] == "ok"
    [rec] = manifest["stages"]
    assert rec["status"] == "ok"
    assert rec["rows"] == 2                                   # truncated from 5
    assert any("truncated" in n for n in rec.get("notes", []))   # not silent

    run_dir = tmp_path / "runs" / manifest["run_id"]
    out = pd.read_parquet(run_dir / "outputs" / "load.parquet")
    assert len(out) == 2

    on_disk = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk["run_id"] == manifest["run_id"]
    assert on_disk["status"] == "ok"


def test_per_run_limit_and_offset_slice_and_are_recorded(tmp_path):
    # 5 rows, static `limit: 2` in the stage YAML. The per-run cap wins over
    # the static one, and the offset drops rows BEFORE the cap is applied:
    # offset 1 drops row 0, then limit 3 keeps rows 1-3.
    _make_project(tmp_path)
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, repo_root=tmp_path,
                           limits={"load": 3}, offsets={"load": 1})

    [rec] = manifest["stages"]
    assert rec["rows"] == 3                                   # not the static 2
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


def test_per_run_override_for_unknown_stage_id_fails_loudly(tmp_path):
    _make_project(tmp_path)
    _seed_version(tmp_path)
    with pytest.raises(ValueError, match="unknown stage id"):
        execute_run(tmp_path, repo_root=tmp_path, limits={"nope": 3})
    with pytest.raises(ValueError, match="unknown stage id"):
        execute_run(tmp_path, repo_root=tmp_path, offsets={"nope": 1})


def _two_stage_project(root, rows: list[dict]):
    """input_data loading `rows` from CSV, feeding an identity
    python_frame_function. Exercises the runner's per-stage input checks."""
    (root / "compiled").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    pd.DataFrame(rows).to_csv(root / "data" / "items.csv", index=False)
    load = {
        "id": "load", "name": "Load items", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": "data/items.csv", "format": "csv"}},
    }
    consume = {
        "id": "consume", "name": "Consume items", "type": "python_frame_function",
        "inputs": [{"id": "load"}],
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
    manifest = execute_run(tmp_path, repo_root=tmp_path)

    records = {r["stage_id"]: r for r in manifest["stages"]}
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
    manifest = execute_run(tmp_path, repo_root=tmp_path)
    assert manifest["status"] == "ok"
    records = {r["stage_id"]: r for r in manifest["stages"]}
    assert records["consume"]["status"] == "ok"
    assert records["consume"]["rows"] == 2


def _row_isolation_project(root, code):
    """input_data(csv) feeding a python_row_function whose inline `code` is the
    per-row transform. 4 rows: val = [0, 1, 2, 3]."""
    (root / "compiled").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    pd.DataFrame({"name": [f"r{i}" for i in range(4)], "val": [0, 1, 2, 3]}) \
        .to_csv(root / "data" / "items.csv", index=False)
    load = {
        "id": "load", "name": "Load", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": "data/items.csv", "format": "csv"}},
    }
    xform = {
        "id": "xform", "name": "Transform", "type": "python_row_function",
        "inputs": [{"id": "load"}],
        "function": {"kind": "inline", "code": code},
    }
    (root / "compiled" / "01_load.json").write_text(json.dumps(load), encoding="utf-8")
    (root / "compiled" / "02_xform.json").write_text(json.dumps(xform), encoding="utf-8")


def test_one_bad_row_is_isolated_not_whole_stage(tmp_path):
    # Row with val==0 divides by zero; the other three must still land in the
    # user-facing output, and the failure is recorded durably (never dropped).
    _row_isolation_project(
        tmp_path,
        "def transform(row):\n    return {'name': row['name'], 'd': 100 // int(row['val'])}\n",
    )
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, repo_root=tmp_path)

    # The run completes (not halted, not crashed) but is loudly non-ok.
    assert manifest["status"] == "errors"
    assert manifest["row_errors_total"] == 1

    recs = {r["stage_id"]: r for r in manifest["stages"]}
    x = recs["xform"]
    assert x["status"] == "row_errors"     # partial success, not whole-stage error
    assert x["input_rows"] == 4
    assert x["row_errors"] == 1
    assert x["rows"] == 3                   # user-facing output missing only the bad row

    run_dir = tmp_path / "runs" / manifest["run_id"]

    # User-facing output: schema-conforming, good rows only, in input order.
    out = pd.read_parquet(run_dir / "outputs" / "xform.parquet")
    assert len(out) == 3
    assert list(out["name"]) == ["r1", "r2", "r3"]   # r0 (val 0) dropped
    assert "d" in out.columns and "_error" not in out.columns  # schema not polluted

    # Runtime-internal shadow: 1:1-by-position record of the failure, next to
    # outputs/, so nothing is silently dropped and positional tracing still works.
    shadow = run_dir / x["row_errors_path"]
    assert shadow == run_dir / "errors" / "xform.jsonl"
    lines = [json.loads(ln) for ln in shadow.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    assert lines[0]["input_row"] == 0
    assert lines[0]["status"] == "error"
    assert lines[0]["error"]["type"] == "ZeroDivisionError"


def test_all_rows_erroring_fails_the_whole_stage_loudly(tmp_path):
    # If EVERY row fails, that is indistinguishable from a systemic bug: the
    # stage is marked `error` (not a silent empty pass).
    _row_isolation_project(
        tmp_path, "def transform(row):\n    raise RuntimeError('boom')\n"
    )
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, repo_root=tmp_path)

    assert manifest["status"] == "errors"
    x = {r["stage_id"]: r for r in manifest["stages"]}["xform"]
    assert x["status"] == "error"
    assert x["row_errors"] == 4 and x["input_rows"] == 4
    assert x["error"]["type"] == "AllRowsErrored"
    # The shadow still holds all four errored rows.
    lines = (tmp_path / "runs" / manifest["run_id"] / x["row_errors_path"]) \
        .read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4


def test_clean_run_writes_no_error_shadow(tmp_path):
    _row_isolation_project(
        tmp_path,
        "def transform(row):\n    return {'name': row['name'], 'd': int(row['val'])}\n",
    )
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, repo_root=tmp_path)

    assert manifest["status"] == "ok"
    assert manifest["row_errors_total"] == 0
    x = {r["stage_id"]: r for r in manifest["stages"]}["xform"]
    assert x["status"] == "ok"
    assert x.get("row_errors", 0) == 0
    assert "row_errors_path" not in x
    run_dir = tmp_path / "runs" / manifest["run_id"]
    assert not (run_dir / "errors").exists()   # no shadow dir for a clean run


def test_run_without_a_version_fails_loudly(tmp_path):
    """A run targets an existing version and never creates one: a valid but
    unversioned working copy raises NoVersionToRunError and leaves nothing on
    disk — no run dir, no fabricated version."""
    _make_project(tmp_path)  # valid working copy, but no version created
    with pytest.raises(NoVersionToRunError):
        execute_run(tmp_path, repo_root=tmp_path)
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "versions").exists()


def test_create_version_rejects_invalid_working_copy(tmp_path):
    """create_version strict-loads before it snapshots: an invalid working copy
    raises WorkflowLoadError and writes NOTHING, so no invalid workflow can
    be immortalised as a version."""
    (tmp_path / "compiled").mkdir(parents=True)
    bad = {"id": "load", "name": "Load", "type": "input_data",
           "connector": {"kind": "file", "params": {"format": "csv"}}}  # no path
    (tmp_path / "compiled" / "01_load.json").write_text(
        json.dumps(bad), encoding="utf-8")

    with pytest.raises(WorkflowLoadError) as exc:
        create_version(tmp_path, message="x", reviewer="test")
    assert any("params.path" in i for i in exc.value.issues)
    assert not (tmp_path / "versions").exists()  # snapshotted nothing


def test_invalid_workflow_never_becomes_a_version_and_run_never_pins_stale(tmp_path):
    """Regression for the version-lifecycle bug: a run used to snapshot the
    working copy as a version BEFORE validating it, so an invalid workflow got
    immortalised as 'the latest' and every later default run reloaded that
    poisoned snapshot and failed with a stale error. Now runs never create
    versions and create_version validates first, so the bug is impossible."""
    # Invalid working copy: file connector missing params.path.
    (tmp_path / "compiled").mkdir(parents=True)
    bad = {"id": "load", "name": "Load", "type": "input_data",
           "connector": {"kind": "file", "params": {"format": "csv"}}}
    (tmp_path / "compiled" / "01_load.json").write_text(
        json.dumps(bad), encoding="utf-8")

    # You cannot make a version from it, and it writes nothing.
    with pytest.raises(WorkflowLoadError):
        create_version(tmp_path, message="x", reviewer="test")
    assert not (tmp_path / "versions").exists()

    # A run refuses (no version) and does NOT auto-create one — nothing on disk.
    with pytest.raises(NoVersionToRunError):
        execute_run(tmp_path, repo_root=tmp_path)
    assert not (tmp_path / "versions").exists()
    assert not (tmp_path / "runs").exists()

    # Fix the working copy. A run STILL refuses until a version is created
    # explicitly — it never silently pins to a stale snapshot (there is none).
    (tmp_path / "data").mkdir(parents=True)
    pd.DataFrame({"name": ["a"], "val": [1]}).to_csv(
        tmp_path / "data" / "items.csv", index=False)
    good = {"id": "load", "name": "Load", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": "data/items.csv", "format": "csv"}}}
    (tmp_path / "compiled" / "01_load.json").write_text(
        json.dumps(good), encoding="utf-8")
    with pytest.raises(NoVersionToRunError):
        execute_run(tmp_path, repo_root=tmp_path)

    # Explicit creation, then the run succeeds against that version.
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, repo_root=tmp_path)
    assert manifest["status"] == "ok"

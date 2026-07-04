"""Integration: the runner's row slicing (static `limit:` + per-run
--limit/--offset overrides) with manifest persistence, and the
duplicate-input-row check at every stage boundary.

Builds small file-connector methodologies in a tmp dir, runs them, and checks
that `limit:` truncated the output, that per-run --limit/--offset slice the
output and are recorded as run provenance (not silent), that manifest.json
landed on disk, and that a stage fed exact duplicate full-content rows fails
loudly naming them.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app.runtime.runner import execute_run


def _make_methodology(root):
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
    _make_methodology(tmp_path)
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
    # 5 rows, static `limit: 2` in the stage spec. The per-run cap wins over
    # the static one, and the offset drops rows BEFORE the cap is applied:
    # offset 1 drops row 0, then limit 3 keeps rows 1-3.
    _make_methodology(tmp_path)
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
    _make_methodology(tmp_path)
    with pytest.raises(ValueError, match="unknown stage id"):
        execute_run(tmp_path, repo_root=tmp_path, limits={"nope": 3})
    with pytest.raises(ValueError, match="unknown stage id"):
        execute_run(tmp_path, repo_root=tmp_path, offsets={"nope": 1})


def _two_stage_methodology(root, rows: list[dict]):
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
    _two_stage_methodology(tmp_path, [
        {"name": "a", "val": 1},
        {"name": "b", "val": 2},
        {"name": "a", "val": 1},
    ])
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
    _two_stage_methodology(tmp_path, [
        {"name": "a", "val": 1},
        {"name": "a", "val": 2},
    ])
    manifest = execute_run(tmp_path, repo_root=tmp_path)
    assert manifest["status"] == "ok"
    records = {r["stage_id"]: r for r in manifest["stages"]}
    assert records["consume"]["status"] == "ok"
    assert records["consume"]["rows"] == 2


def test_invalid_stage_rejected_before_run(tmp_path):
    """A contract-invalid stage must fail the run at load — no run dir, no
    partial execution, no empty-dataframe fallback."""
    from app.services.loader import MethodologyLoadError

    (tmp_path / "compiled").mkdir(parents=True)
    bad = {"id": "load", "name": "Load", "type": "input_data",
           "connector": {"kind": "file", "params": {"format": "csv"}}}  # no path
    (tmp_path / "compiled" / "01_load.json").write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(MethodologyLoadError) as exc:
        execute_run(tmp_path, repo_root=tmp_path)
    assert any("params.path" in i for i in exc.value.issues)
    assert not (tmp_path / "runs").exists()  # nothing was created

"""The runner's two between-run-loop cancellation behaviors:
  - a cancel requested before any stage starts leaves everything pending
  - a cancel requested between two stages preserves the completed stage's
    output on disk and marks the rest 'pending'
Both go through the real prepare_run/run_prepared entry points so the
manifest shape matches what a live run would produce. See
app/runtime/cancellation.py for the request/poll design and
app/runtime/runner.py::_execute_stages for the checkpoints under test.
"""
from __future__ import annotations

import json

import pandas as pd

import app.runtime.runner as runner
from app.runtime.cancellation import is_cancelled, request_cancel
from app.runtime.runner import prepare_run, run_prepared
from app.services import versioning
from app.services.versioning import create_version_from_disk


def _seed_version(root):
    vid = create_version_from_disk(root, message="test seed", reviewer="test").version_id
    versioning.publish_version(root, vid, reviewer="human")
    return vid


def _one_stage_project(root):
    (root / "compiled").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}).to_csv(
        root / "data" / "items.csv", index=False)
    stage = {"id": "load", "name": "Load items", "type": "input_data",
             "connector": {"kind": "file",
                           "params": {"path": str(root / "data" / "items.csv"), "format": "csv"}}}
    (root / "compiled" / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")


def _two_stage_project(root):
    _one_stage_project(root)
    consume = {"id": "consume", "name": "Consume items", "type": "python_frame_function",
               "inputs": [{"id": "load"}],
               "function": {"kind": "inline",
                            "code": "def transform(df):\n    return df\n"}}
    (root / "compiled" / "02_consume.json").write_text(json.dumps(consume), encoding="utf-8")


def test_cancel_requested_before_run_starts_leaves_the_first_stage_pending(tmp_path):
    _one_stage_project(tmp_path)
    _seed_version(tmp_path)
    prep = prepare_run(tmp_path, repo_root=tmp_path)
    request_cancel(tmp_path.name, prep["run_id"])

    manifest = run_prepared(prep)

    assert manifest["status"] == "cancelled"
    assert manifest["cancelled_at"] == "load"
    [rec] = manifest["stages"]
    assert rec["status"] == "pending"

    run_dir = tmp_path / "runs" / prep["run_id"]
    assert not (run_dir / "outputs" / "load.parquet").exists()
    # the registry entry does not leak past the run's own end
    assert is_cancelled(tmp_path.name, prep["run_id"]) is False


def test_mid_run_cancel_preserves_the_completed_stages_output(tmp_path, monkeypatch):
    """Cancellation arrives between stage 1 ('load') and stage 2 ('consume').
    Simulated deterministically — is_cancelled flips False -> True exactly
    between the two between-stage checkpoint calls — instead of coordinating
    real threads, which would make the test timing-dependent."""
    _two_stage_project(tmp_path)
    _seed_version(tmp_path)
    prep = prepare_run(tmp_path, repo_root=tmp_path)

    calls = {"n": 0}

    def fake_is_cancelled(project: str, run_id: str) -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # False for stage 1's checkpoint, True from stage 2's on

    monkeypatch.setattr(runner, "is_cancelled", fake_is_cancelled)

    manifest = run_prepared(prep)

    assert manifest["status"] == "cancelled"
    assert manifest["cancelled_at"] == "consume"
    records = {r["stage_id"]: r for r in manifest["stages"]}
    assert records["load"]["status"] == "ok"
    assert records["consume"]["status"] == "pending"

    run_dir = tmp_path / "runs" / prep["run_id"]
    assert (run_dir / "outputs" / "load.parquet").exists()
    assert not (run_dir / "outputs" / "consume.parquet").exists()

    on_disk = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk["status"] == "cancelled"
    assert on_disk["cancelled_at"] == "consume"

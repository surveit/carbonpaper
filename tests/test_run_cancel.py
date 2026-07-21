"""The runner's three cancellation behaviors:
  - a cancel requested before any stage starts leaves everything pending
  - a cancel requested between two stages preserves the completed stage's
    output on disk and marks the rest 'pending'
  - a cancel requested DURING a stage's row-mapped fan-out marks that stage
    itself 'cancelled' (not 'pending'), distinct from the between-stage case
All three go through the real prepare_run/run_prepared entry points so the
manifest shape matches what a live run would produce. A fourth test shows a
cancelled run is not terminal: it can be resumed and run to completion, because
the cancel message was consumed (not left as a lingering flag). See
app/runtime/cancellation.py for the request/consume design and
app/runtime/runner.py::_execute_stages for the checkpoints under test.
"""
from __future__ import annotations

import json

import pandas as pd

import app.runtime.runner as runner
import app.runtime.stages.execution as execution
from app.runtime.cancellation import consume_cancel, request_cancel
from app.runtime.runner import prepare_run, run_prepared
from app.runtime.stages import llm_transform as lt
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
    # Consume-on-read: the run popped the cancel message at its first checkpoint,
    # so the mailbox is now empty. That is exactly what lets the same run be
    # resumed — see test_a_cancelled_run_can_be_resumed_and_runs_to_completion.
    assert consume_cancel(tmp_path.name, prep["run_id"]) is False


def test_mid_run_cancel_preserves_the_completed_stages_output(tmp_path, monkeypatch):
    """Cancellation arrives between stage 1 ('load') and stage 2 ('consume').
    Simulated deterministically — consume_cancel returns False for stage 1's
    checkpoint and True from stage 2's on — instead of coordinating real
    threads, which would make the test timing-dependent."""
    _two_stage_project(tmp_path)
    _seed_version(tmp_path)
    prep = prepare_run(tmp_path, repo_root=tmp_path)

    calls = {"n": 0}

    def fake_consume_cancel(project: str, run_id: str) -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # nothing at stage 1's checkpoint, a message from stage 2's on

    monkeypatch.setattr(runner, "consume_cancel", fake_consume_cancel)

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


def _three_stage_llm_project(root):
    """input_data 'load' (5 rows) -> llm_transform 'score' (row-mapped, fans
    out under parallelism > 1) -> a python_frame_function 'downstream' stage.
    Unlike _two_stage_project's FrameHandler-only 'consume' (which never
    enters the row driver), 'score' is driven by execution.py's row mapper —
    the mid-fan-out cancellation checkpoint under test lives there."""
    (root / "compiled").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    pd.DataFrame({"id": [f"r{i}" for i in range(5)], "text": ["hi"] * 5}).to_csv(
        root / "data" / "items.csv", index=False)
    load = {
        "id": "load", "name": "Load items", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(root / "data" / "items.csv"), "format": "csv"}},
    }
    score = {
        "id": "score", "name": "Score items", "type": "llm_transform",
        "inputs": [{"id": "load", "schema": {
            "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}],
            "primary_key": ["id"]}}],
        "output_schema": {
            "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"},
                        {"name": "score", "type": "int", "nullable": False}],
            "primary_key": ["id"]},
        "llm": {"prompt_template": "Rate: {text}"},
    }
    downstream = {
        "id": "downstream", "name": "Downstream", "type": "python_frame_function",
        "inputs": [{"id": "score"}],
        "function": {"kind": "inline", "code": "def transform(df):\n    return df\n"},
    }
    (root / "compiled" / "01_load.json").write_text(json.dumps(load), encoding="utf-8")
    (root / "compiled" / "02_score.json").write_text(json.dumps(score), encoding="utf-8")
    (root / "compiled" / "03_downstream.json").write_text(json.dumps(downstream), encoding="utf-8")


def test_mid_stage_cancel_marks_the_running_stage_cancelled_not_pending(tmp_path, monkeypatch):
    """Cancellation arrives DURING 'score's row-mapper fan-out, not between
    stages: execution.py's consume_cancel binding is forced True so the row
    driver raises RunCancelled mid-fan-out, while runner.py's own consume_cancel
    binding stays real (no message was requested) so 'score' starts rather than
    being skipped by the between-stage checkpoint above. Exercises the runner's
    `except RunCancelled:` branch: the stage that was RUNNING is recorded
    'cancelled', distinct from a not-yet-started stage's 'pending'."""
    def fake_call_llm(stage_id, llm_config, row, **kw):
        return {"score": 1}

    monkeypatch.setattr(lt, "call_llm", fake_call_llm)
    monkeypatch.setattr(execution, "consume_cancel", lambda project, run_id: True)

    _three_stage_llm_project(tmp_path)
    _seed_version(tmp_path)
    prep = prepare_run(tmp_path, repo_root=tmp_path)

    manifest = run_prepared(prep)

    assert manifest["status"] == "cancelled"
    assert manifest["cancelled_at"] == "score"
    records = {r["stage_id"]: r for r in manifest["stages"]}
    assert records["load"]["status"] == "ok"
    assert records["score"]["status"] == "cancelled"
    assert records["downstream"]["status"] == "pending"

    run_dir = tmp_path / "runs" / prep["run_id"]
    assert (run_dir / "outputs" / "load.parquet").exists()
    assert not (run_dir / "outputs" / "score.parquet").exists()
    assert not (run_dir / "outputs" / "downstream.parquet").exists()

    on_disk = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk["status"] == "cancelled"
    assert on_disk["cancelled_at"] == "score"


def test_a_cancelled_run_can_be_resumed_and_runs_to_completion(tmp_path):
    """Cancel is a consumed signal, not a terminal state. A run cancelled before
    it starts can be resumed: the cancelled run popped the message, so the resume
    finds an empty mailbox and runs every stage to 'ok'. Resuming needs no
    special-casing of the cancel — that is the whole point of consume-on-read."""
    _two_stage_project(tmp_path)
    _seed_version(tmp_path)
    prep = prepare_run(tmp_path, repo_root=tmp_path)
    request_cancel(tmp_path.name, prep["run_id"])

    cancelled = run_prepared(prep)
    assert cancelled["status"] == "cancelled"

    resumed = runner.resume_run(tmp_path, prep["run_id"], tmp_path)

    assert resumed["status"] == "ok"
    records = {r["stage_id"]: r for r in resumed["stages"]}
    assert records["load"]["status"] == "ok"
    assert records["consume"]["status"] == "ok"
    run_dir = tmp_path / "runs" / prep["run_id"]
    assert (run_dir / "outputs" / "load.parquet").exists()
    assert (run_dir / "outputs" / "consume.parquet").exists()

from __future__ import annotations


import pandas as pd

import app.runtime.executor as executor
import app.runtime.runner as runner
import app.runtime.stages.execution as execution
from app.runtime.cancellation import consume_cancel, request_cancel
from app.runtime.runner import prepare_run, run_prepared
from app.runtime.stages import llm_transform as lt
from app.services import versioning
from app.services.project import save_working_copy_as_version
from conftest import pinned_stages, resumed_stages
from stage_seed import add_stage
from run_seed import read_manifest


# The two shapes the fixtures below load: the (name, val) items csv, and the
# (id, text) csv the llm project scores. Declared once so an upstream's
# output_schema and its downstream's input `schema` cannot drift apart.
_NAME_VAL_SCHEMA = {"columns": [{"name": "name", "type": "str", "nullable": True},
                                {"name": "val", "type": "int", "nullable": True}]}
_ID_TEXT_SCHEMA = {"columns": [{"name": "id", "type": "str", "nullable": True},
                               {"name": "text", "type": "str", "nullable": True}]}
_SCORED_SCHEMA = {"columns": [{"name": "id", "type": "str", "nullable": True},
                              {"name": "text", "type": "str", "nullable": True},
                              {"name": "score", "type": "int", "nullable": False}]}


def _seed_version(root):
    vid = save_working_copy_as_version(root, message="test seed", reviewer="test").version_id
    versioning.publish_version(root, vid, reviewer="human")
    return vid


def _one_stage_project(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}).to_csv(
        root / "data" / "items.csv", index=False)
    stage = {"id": "load", "description": "Load items", "type": "input_data",
             "connector": {"kind": "file",
                           "params": {"path": str(root / "data" / "items.csv"), "format": "csv"}},
             "signature": {"form": "replaces", "produces": _NAME_VAL_SCHEMA["columns"]}}
    add_stage(root, stage)


def _two_stage_project(root):
    _one_stage_project(root)
    consume = {"id": "consume", "description": "Consume items", "type": "python_frame_function",
               "inputs": [{"id": "load", "schema": _NAME_VAL_SCHEMA}],
               "signature": {
                   "form": "replaces",
                   "reads": [{"input": "load", "columns": _NAME_VAL_SCHEMA["columns"]}],
                   "produces": _NAME_VAL_SCHEMA["columns"],
               },
               "function": {"kind": "inline",
                            "code": "def transform(df):\n    return df\n"}}
    add_stage(root, consume)


def test_cancel_requested_before_run_starts_leaves_the_first_stage_pending(tmp_path):
    _one_stage_project(tmp_path)
    _seed_version(tmp_path)
    prep = prepare_run(tmp_path, tmp_path, *pinned_stages(tmp_path))
    request_cancel(tmp_path.name, prep["run_id"])

    manifest = run_prepared(prep)

    assert manifest["status"] == "cancelled"
    assert manifest["cancelled_at"] == "load"
    [rec] = manifest["stage_records"]
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
    prep = prepare_run(tmp_path, tmp_path, *pinned_stages(tmp_path))

    calls = {"n": 0}

    def fake_consume_cancel(project: str, run_id: str) -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # nothing at stage 1's checkpoint, a message from stage 2's on

    monkeypatch.setattr(executor, "consume_cancel", fake_consume_cancel)

    manifest = run_prepared(prep)

    assert manifest["status"] == "cancelled"
    assert manifest["cancelled_at"] == "consume"
    records = {r["stage_id"]: r for r in manifest["stage_records"]}
    assert records["load"]["status"] == "ok"
    assert records["consume"]["status"] == "pending"

    run_dir = tmp_path / "runs" / prep["run_id"]
    assert (run_dir / "outputs" / "load.parquet").exists()
    assert not (run_dir / "outputs" / "consume.parquet").exists()

    on_disk = read_manifest(run_dir.parent.parent, run_dir.name)
    assert on_disk["status"] == "cancelled"
    assert on_disk["cancelled_at"] == "consume"


def _three_stage_llm_project(root):
    """input_data 'load' (5 rows) -> llm_transform 'score' (row-mapped, fans
    out under parallelism > 1) -> a python_frame_function 'downstream' stage.
    Unlike _two_stage_project's FrameHandler-only 'consume' (which never
    enters the row driver), 'score' is driven by execution.py's row mapper —
    the mid-fan-out cancellation checkpoint under test lives there."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"id": [f"r{i}" for i in range(5)], "text": ["hi"] * 5}).to_csv(
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
    downstream = {
        "id": "downstream", "description": "Downstream", "type": "python_frame_function",
        "inputs": [{"id": "score", "schema": _SCORED_SCHEMA}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": "score", "columns": _SCORED_SCHEMA["columns"]}],
            "produces": _SCORED_SCHEMA["columns"],
        },
        "function": {"kind": "inline", "code": "def transform(df):\n    return df\n"},
    }
    add_stage(root, load)
    add_stage(root, score)
    add_stage(root, downstream)


def test_mid_stage_cancel_marks_the_running_stage_cancelled_not_pending(tmp_path, monkeypatch):
    """Cancellation arrives DURING 'score's row-mapper fan-out, not between
    stages: execution.py's consume_cancel binding is forced True so the row
    driver raises RunCancelled mid-fan-out, while executor.py's own consume_cancel
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
    prep = prepare_run(tmp_path, tmp_path, *pinned_stages(tmp_path))

    manifest = run_prepared(prep)

    assert manifest["status"] == "cancelled"
    assert manifest["cancelled_at"] == "score"
    records = {r["stage_id"]: r for r in manifest["stage_records"]}
    assert records["load"]["status"] == "ok"
    assert records["score"]["status"] == "cancelled"
    assert records["downstream"]["status"] == "pending"

    run_dir = tmp_path / "runs" / prep["run_id"]
    assert (run_dir / "outputs" / "load.parquet").exists()
    assert not (run_dir / "outputs" / "score.parquet").exists()
    assert not (run_dir / "outputs" / "downstream.parquet").exists()

    on_disk = read_manifest(run_dir.parent.parent, run_dir.name)
    assert on_disk["status"] == "cancelled"
    assert on_disk["cancelled_at"] == "score"


def test_a_cancelled_run_can_be_resumed_and_runs_to_completion(tmp_path):
    """Cancel is a consumed signal, not a terminal state. A run cancelled before
    it starts can be resumed: the cancelled run popped the message, so the resume
    finds an empty mailbox and runs every stage to 'ok'. Resuming needs no
    special-casing of the cancel — that is the whole point of consume-on-read."""
    _two_stage_project(tmp_path)
    _seed_version(tmp_path)
    prep = prepare_run(tmp_path, tmp_path, *pinned_stages(tmp_path))
    request_cancel(tmp_path.name, prep["run_id"])

    cancelled = run_prepared(prep)
    assert cancelled["status"] == "cancelled"

    resumed = runner.resume_run(tmp_path, prep["run_id"], tmp_path,
                            *resumed_stages(tmp_path, prep["run_id"]))

    assert resumed["status"] == "ok"
    records = {r["stage_id"]: r for r in resumed["stage_records"]}
    assert records["load"]["status"] == "ok"
    assert records["consume"]["status"] == "ok"
    run_dir = tmp_path / "runs" / prep["run_id"]
    assert (run_dir / "outputs" / "load.parquet").exists()
    assert (run_dir / "outputs" / "consume.parquet").exists()

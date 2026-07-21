"""The runner's loop control on error and halt: both are fork-blocking, not
loop-ending. A stage's `ok` status asserts that every upstream stage actually
succeeded, so the runner must never run a stage on a failed/halted upstream's
output and mark it `ok`.

  - an errored stage blocks only its transitive downstream (those stay
    `pending`, no output written); independent forks run to completion
  - a halting human_review_queue stage does the same, and multiple halts each
    block only their own downstream
  - a run with both an error and a halt reports `errors` overall while the
    halted stage still reads `awaiting_review`
  - resume after an error re-runs the errored stage AND its downstream (their
    outputs are never reused as stale)

Cancel's hard-stop behaviour is unchanged and covered by test_run_cancel.py.
DAGs are built the way test_run_cancel.py builds them (compiled/*.json +
_seed_version; run offline via prepare_run/run_prepared; mock LLM forced by
conftest.py).
"""
from __future__ import annotations

import json

import pandas as pd

import app.runtime.runner as runner
from app.runtime.runner import prepare_run, run_prepared
from app.services import versioning
from app.services.versioning import create_version_from_disk


def _seed_version(root):
    vid = create_version_from_disk(root, message="test seed", reviewer="test").version_id
    versioning.publish_version(root, vid, reviewer="human")
    return vid


def _write_stage(root, filename, stage):
    (root / "compiled").mkdir(parents=True, exist_ok=True)
    (root / "compiled" / filename).write_text(json.dumps(stage), encoding="utf-8")


def _load_items_stage(root, *, stage_id="load"):
    """An input_data stage reading a 2-row csv the test writes to disk."""
    (root / "data").mkdir(parents=True, exist_ok=True)
    csv_path = root / "data" / f"{stage_id}.csv"
    pd.DataFrame({"id": ["a", "b"], "val": [1, 2]}).to_csv(csv_path, index=False)
    return {"id": stage_id, "name": f"Load {stage_id}", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": str(csv_path), "format": "csv"}}}


def _raising_stage(stage_id, input_id, name="Boom"):
    """A python_frame_function whose transform raises — the stage errors."""
    return {"id": stage_id, "name": name, "type": "python_frame_function",
            "inputs": [{"id": input_id}],
            "function": {"kind": "inline",
                         "code": "def transform(df):\n    raise ValueError('boom')\n"}}


def _passthrough_stage(stage_id, input_id, name="Passthrough"):
    return {"id": stage_id, "name": name, "type": "python_frame_function",
            "inputs": [{"id": input_id}],
            "function": {"kind": "inline",
                         "code": "def transform(df):\n    return df\n"}}


def _queue_stage(stage_id, input_id, name="Review"):
    """A human_review_queue with no prior decisions on disk — it halts."""
    return {"id": stage_id, "name": name, "type": "human_review_queue",
            "inputs": [{"id": input_id, "schema": {
                "columns": [{"name": "id", "type": "str"}, {"name": "val", "type": "int"}],
                "primary_key": ["id"]}}],
            "queue": {"hash_columns": ["id"]}}


def _stage_status(manifest, stage_id):
    for record in manifest["stages"]:
        if record["stage_id"] == stage_id:
            return record["status"]
    raise AssertionError(f"stage {stage_id!r} not in manifest")


# ── Error blocks downstream only ─────────────────────────────────────────────

def test_error_blocks_transitive_downstream_in_a_chain(tmp_path):
    """load -> boom (errors) -> tail. boom errors, so tail never runs: it stays
    pending with no output file, and the run reports errors."""
    _write_stage(tmp_path, "01_load.json", _load_items_stage(tmp_path))
    _write_stage(tmp_path, "02_boom.json", _raising_stage("boom", "load"))
    _write_stage(tmp_path, "03_tail.json", _passthrough_stage("tail", "boom"))
    _seed_version(tmp_path)

    manifest = run_prepared(prepare_run(tmp_path, repo_root=tmp_path))

    assert manifest["status"] == "errors"
    assert _stage_status(manifest, "load") == "ok"
    assert _stage_status(manifest, "boom") == "error"
    assert _stage_status(manifest, "tail") == "pending"

    outputs = tmp_path / "runs" / manifest["run_id"] / "outputs"
    assert (outputs / "load.parquet").exists()
    assert not (outputs / "boom.parquet").exists()
    assert not (outputs / "tail.parquet").exists()


def test_error_in_one_fork_lets_the_independent_fork_finish(tmp_path):
    """load -> {boom (errors) -> boom_tail} and load -> good_tail. The good fork
    runs to ok; only boom's own downstream is blocked."""
    _write_stage(tmp_path, "01_load.json", _load_items_stage(tmp_path))
    _write_stage(tmp_path, "02_boom.json", _raising_stage("boom", "load"))
    _write_stage(tmp_path, "03_boom_tail.json", _passthrough_stage("boom_tail", "boom"))
    _write_stage(tmp_path, "04_good.json", _passthrough_stage("good_tail", "load"))
    _seed_version(tmp_path)

    manifest = run_prepared(prepare_run(tmp_path, repo_root=tmp_path))

    assert manifest["status"] == "errors"
    assert _stage_status(manifest, "load") == "ok"
    assert _stage_status(manifest, "boom") == "error"
    assert _stage_status(manifest, "boom_tail") == "pending"
    assert _stage_status(manifest, "good_tail") == "ok"

    outputs = tmp_path / "runs" / manifest["run_id"] / "outputs"
    assert (outputs / "good_tail.parquet").exists()
    assert not (outputs / "boom_tail.parquet").exists()


# ── Halt is fork-aware ───────────────────────────────────────────────────────

def test_halt_in_one_fork_lets_the_independent_fork_finish(tmp_path):
    """load -> {review (halts) -> review_tail} and load -> good_tail. The halted
    stage is awaiting_review, its downstream pending, the independent fork ok,
    and the run is awaiting_review."""
    _write_stage(tmp_path, "01_load.json", _load_items_stage(tmp_path))
    _write_stage(tmp_path, "02_review.json", _queue_stage("review", "load"))
    _write_stage(tmp_path, "03_review_tail.json", _passthrough_stage("review_tail", "review"))
    _write_stage(tmp_path, "04_good.json", _passthrough_stage("good_tail", "load"))
    _seed_version(tmp_path)

    manifest = run_prepared(prepare_run(tmp_path, repo_root=tmp_path))

    assert manifest["status"] == "awaiting_review"
    assert manifest["halted_at"] == ["review"]
    assert _stage_status(manifest, "review") == "awaiting_review"
    assert _stage_status(manifest, "review_tail") == "pending"
    assert _stage_status(manifest, "good_tail") == "ok"

    outputs = tmp_path / "runs" / manifest["run_id"] / "outputs"
    assert (outputs / "good_tail.parquet").exists()
    assert not (outputs / "review_tail.parquet").exists()


def test_two_parallel_halts_each_block_only_their_own_downstream(tmp_path):
    """load -> {review_a (halts) -> tail_a} and load -> {review_b (halts) ->
    tail_b}. Both queue stages halt; each blocks only its own tail; halted_at
    lists both."""
    _write_stage(tmp_path, "01_load.json", _load_items_stage(tmp_path))
    _write_stage(tmp_path, "02_review_a.json", _queue_stage("review_a", "load"))
    _write_stage(tmp_path, "03_tail_a.json", _passthrough_stage("tail_a", "review_a"))
    _write_stage(tmp_path, "04_review_b.json", _queue_stage("review_b", "load"))
    _write_stage(tmp_path, "05_tail_b.json", _passthrough_stage("tail_b", "review_b"))
    _seed_version(tmp_path)

    manifest = run_prepared(prepare_run(tmp_path, repo_root=tmp_path))

    assert manifest["status"] == "awaiting_review"
    assert set(manifest["halted_at"]) == {"review_a", "review_b"}
    assert _stage_status(manifest, "review_a") == "awaiting_review"
    assert _stage_status(manifest, "review_b") == "awaiting_review"
    assert _stage_status(manifest, "tail_a") == "pending"
    assert _stage_status(manifest, "tail_b") == "pending"


# ── Mixed error + halt ───────────────────────────────────────────────────────

def test_error_and_halt_together_report_errors_but_keep_stage_awaiting_review(tmp_path):
    """load -> {boom (errors)} and load -> {review (halts)}. The overall run is
    errors (error wins), while the halted stage still reads awaiting_review and
    is listed in halted_at."""
    _write_stage(tmp_path, "01_load.json", _load_items_stage(tmp_path))
    _write_stage(tmp_path, "02_boom.json", _raising_stage("boom", "load"))
    _write_stage(tmp_path, "03_review.json", _queue_stage("review", "load"))
    _seed_version(tmp_path)

    manifest = run_prepared(prepare_run(tmp_path, repo_root=tmp_path))

    assert manifest["status"] == "errors"
    assert _stage_status(manifest, "boom") == "error"
    assert _stage_status(manifest, "review") == "awaiting_review"
    assert manifest["halted_at"] == ["review"]


# ── Resume after error is not stale ──────────────────────────────────────────

def test_resume_after_error_reruns_the_errored_stage_and_its_downstream(tmp_path):
    """load -> mid -> tail. `load` passes preflight (a valid csv exists) but is
    then truncated to empty before execution, so it errors at read time. Its
    downstream mid + tail stay pending — never marked `ok` on `load`'s absent
    output — so on resume they re-run against `load`'s real data rather than
    reusing a stale empty frame. This is the stale-reuse bug the invariant closes.

    prepare_run and run_prepared are called separately so the file can be
    corrupted in between: preflight (in prepare_run) sees the valid file, the
    handler (in run_prepared) sees the empty one."""
    (tmp_path / "data").mkdir(parents=True)
    csv_path = tmp_path / "data" / "items.csv"
    pd.DataFrame({"id": ["a", "b"], "val": [1, 2]}).to_csv(csv_path, index=False)
    load = {"id": "load", "name": "Load", "type": "input_data",
            "connector": {"kind": "file", "params": {"path": str(csv_path), "format": "csv"}}}
    _write_stage(tmp_path, "01_load.json", load)
    _write_stage(tmp_path, "02_mid.json", _passthrough_stage("mid", "load"))
    _write_stage(tmp_path, "03_tail.json", _passthrough_stage("tail", "mid"))
    _seed_version(tmp_path)

    prep = prepare_run(tmp_path, repo_root=tmp_path)  # preflight hashes the valid file
    csv_path.write_text("", encoding="utf-8")          # now empty -> read errors at run
    first = run_prepared(prep)

    assert first["status"] == "errors"
    assert _stage_status(first, "load") == "error"
    assert _stage_status(first, "mid") == "pending"
    assert _stage_status(first, "tail") == "pending"
    outputs = tmp_path / "runs" / first["run_id"] / "outputs"
    assert not (outputs / "load.parquet").exists()
    assert not (outputs / "mid.parquet").exists()
    assert not (outputs / "tail.parquet").exists()

    # Restore the input so load can succeed, then resume the same run.
    pd.DataFrame({"id": ["a", "b"], "val": [1, 2]}).to_csv(csv_path, index=False)
    resumed = runner.resume_run(tmp_path, first["run_id"], tmp_path)

    assert resumed["status"] == "ok"
    assert _stage_status(resumed, "load") == "ok"
    assert _stage_status(resumed, "mid") == "ok"
    assert _stage_status(resumed, "tail") == "ok"
    tail_out = pd.read_parquet(outputs / "tail.parquet")
    assert list(tail_out["val"]) == [1, 2]  # real data, not a stale empty frame

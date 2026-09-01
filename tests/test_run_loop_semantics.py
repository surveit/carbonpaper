from __future__ import annotations


import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.runtime.executor as executor
import app.runtime.runner as runner
import app.web.loading as loading
from app.core.errors import RunVersionUnresolvableError
from app.main import app
from app.runtime.runner import prepare_run, run_prepared
from app.runtime.stages import llm_transform as lt
from app.services import run as run_service
from app.services.errors import WorkflowLoadError
from app.services.loader import load_workflow
from app.services.project import save_working_copy_as_version
from app.services import workspace
from conftest import pinned_stages, queue_added_columns, queue_columns, resumed_stages
from stage_seed import add_stage
from run_seed import read_manifest, store_manifest


# The three frame shapes this file's DAGs carry. Declared once so an upstream's
# output_schema and its downstream's input `schema` cannot drift apart.
_ID_VAL_COLUMNS = [{"name": "id", "type": "str", "nullable": True},
                   {"name": "val", "type": "int", "nullable": True}]
_ID_VAL_SCHEMA = {"columns": _ID_VAL_COLUMNS}
_QUEUE_OUT_SCHEMA = {"columns": _ID_VAL_COLUMNS + queue_added_columns("human_val")}
_ID_TEXT_SCHEMA = {"columns": [{"name": "id", "type": "str", "nullable": True},
                               {"name": "text", "type": "str", "nullable": True}]}
_SCORED_SCHEMA = {"columns": [{"name": "id", "type": "str", "nullable": True},
                              {"name": "text", "type": "str", "nullable": True},
                              {"name": "score", "type": "int", "nullable": False}]}


def _seed_version(root):
    vid = save_working_copy_as_version(root.name, message="test seed").version_id
    return vid


def _write_stage(root, filename, stage):
    root.mkdir(parents=True, exist_ok=True)
    add_stage(root, stage)


def _load_items_stage(root, *, stage_id="load"):
    (root / "data").mkdir(parents=True, exist_ok=True)
    csv_path = root / "data" / f"{stage_id}.csv"
    pd.DataFrame({"id": ["a", "b"], "val": [1, 2]}).to_csv(csv_path, index=False)
    return {"id": stage_id, "description": f"Load {stage_id}", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": str(csv_path), "format": "csv"}},
            "signature": {"form": "replaces", "produces": _ID_VAL_COLUMNS}}


def _raising_stage(stage_id, input_id, name="Boom", schema=_ID_VAL_SCHEMA):
    return {"id": stage_id, "description": name, "type": "python_frame_function",
            "inputs": [{"id": input_id}],
            "signature": {"form": "replaces", "produces": schema["columns"]},
            "function": {"kind": "inline",
                         "code": "def transform(df):\n    raise ValueError('boom')\n"}}


def _passthrough_stage(stage_id, input_id, name="Passthrough", schema=_ID_VAL_SCHEMA):
    return {"id": stage_id, "description": name, "type": "python_frame_function",
            "inputs": [{"id": input_id}],
            "signature": {"form": "replaces", "produces": schema["columns"]},
            "function": {"kind": "inline",
                         "code": "def transform(df):\n    return df\n"}}


def _score_load_stage(root):
    (root / "data").mkdir(parents=True, exist_ok=True)
    csv_path = root / "data" / "score_items.csv"
    pd.DataFrame({"id": ["a", "b"], "text": ["x", "y"]}).to_csv(csv_path, index=False)
    return {"id": "load", "description": "Load", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": str(csv_path), "format": "csv"}},
            "signature": {"form": "replaces", "produces": _ID_TEXT_SCHEMA["columns"]}}


def _score_stage(stage_id, input_id, name="Score"):
    return {"id": stage_id, "description": name, "type": "llm_transform",
            "inputs": [{"id": input_id}],
            "signature": {
                "form": "extends",
                "reads": [{"input": input_id, "columns": [
                    {"name": "text", "type": "str", "nullable": True}]}],
                "adds": [{"name": "score", "type": "int", "nullable": False}]},
            "llm": {"prompt_template": "Rate: {text}"}}


def _queue_stage(stage_id, input_id, name="Review"):
    return {"id": stage_id, "description": name, "type": "human_review_queue",
            "inputs": [{"id": input_id}],
            "signature": {"form": "extends",
                          "reads": [{"input": input_id, "columns": _ID_VAL_SCHEMA["columns"]}],
                          "adds": queue_added_columns("human_val")},
            "queue": queue_columns("val", "human_val")}


def _five_item_load_stage(root):
    (root / "data").mkdir(parents=True, exist_ok=True)
    csv_path = root / "data" / "five_items.csv"
    pd.DataFrame({"id": list("abcde"), "val": [1, 2, 3, 4, 5]}).to_csv(csv_path, index=False)
    return {"id": "load", "description": "Load", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": str(csv_path), "format": "csv"}},
            "signature": {"form": "replaces", "produces": _ID_VAL_COLUMNS}}


def _filtered_queue_stage(stage_id, input_id, flt, name="Review"):
    return {"id": stage_id, "description": name, "type": "human_review_queue",
            "inputs": [{"id": input_id}],
            "signature": {"form": "extends",
                          "reads": [{"input": input_id, "columns": _ID_VAL_SCHEMA["columns"]}],
                          "adds": queue_added_columns("human_val")},
            "queue": {**queue_columns("val", "human_val"), "filter": flt}}


def _stage_status(manifest, stage_id):
    for record in manifest["stage_records"]:
        if record["stage_id"] == stage_id:
            return record["status"]
    raise AssertionError(f"stage {stage_id!r} not in manifest")


# ── Error blocks downstream only ─────────────────────────────────────────────

def test_error_blocks_transitive_downstream_in_a_chain(tmp_path):
    _write_stage(tmp_path, "01_load.json", _load_items_stage(tmp_path))
    _write_stage(tmp_path, "02_boom.json", _raising_stage("boom", "load"))
    _write_stage(tmp_path, "03_tail.json", _passthrough_stage("tail", "boom"))
    _seed_version(tmp_path)

    manifest = run_prepared(prepare_run(tmp_path / "runs", tmp_path.name, *pinned_stages(tmp_path)))

    assert manifest["status"] == "errors"
    assert _stage_status(manifest, "load") == "ok"
    assert _stage_status(manifest, "boom") == "error"
    assert _stage_status(manifest, "tail") == "pending"

    outputs = tmp_path / "runs" / manifest["run_id"] / "outputs"
    assert (outputs / "load.parquet").exists()
    assert not (outputs / "boom.parquet").exists()
    assert not (outputs / "tail.parquet").exists()


def test_error_in_one_fork_lets_the_independent_fork_finish(tmp_path):
    _write_stage(tmp_path, "01_load.json", _load_items_stage(tmp_path))
    _write_stage(tmp_path, "02_boom.json", _raising_stage("boom", "load"))
    _write_stage(tmp_path, "03_boom_tail.json", _passthrough_stage("boom_tail", "boom"))
    _write_stage(tmp_path, "04_good.json", _passthrough_stage("good_tail", "load"))
    _seed_version(tmp_path)

    manifest = run_prepared(prepare_run(tmp_path / "runs", tmp_path.name, *pinned_stages(tmp_path)))

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
    _write_stage(tmp_path, "01_load.json", _load_items_stage(tmp_path))
    _write_stage(tmp_path, "02_review.json", _queue_stage("review", "load"))
    _write_stage(tmp_path, "03_review_tail.json", _passthrough_stage("review_tail", "review"))
    _write_stage(tmp_path, "04_good.json", _passthrough_stage("good_tail", "load"))
    _seed_version(tmp_path)

    manifest = run_prepared(prepare_run(tmp_path / "runs", tmp_path.name, *pinned_stages(tmp_path)))

    assert manifest["status"] == "awaiting_review"
    assert manifest["halted_at"] == ["review"]
    assert _stage_status(manifest, "review") == "awaiting_review"
    assert _stage_status(manifest, "review_tail") == "pending"
    assert _stage_status(manifest, "good_tail") == "ok"

    outputs = tmp_path / "runs" / manifest["run_id"] / "outputs"
    assert (outputs / "good_tail.parquet").exists()
    assert not (outputs / "review_tail.parquet").exists()


def test_two_parallel_halts_each_block_only_their_own_downstream(tmp_path):
    _write_stage(tmp_path, "01_load.json", _load_items_stage(tmp_path))
    _write_stage(tmp_path, "02_review_a.json", _queue_stage("review_a", "load"))
    _write_stage(tmp_path, "03_tail_a.json", _passthrough_stage("tail_a", "review_a"))
    _write_stage(tmp_path, "04_review_b.json", _queue_stage("review_b", "load"))
    _write_stage(tmp_path, "05_tail_b.json", _passthrough_stage("tail_b", "review_b"))
    _seed_version(tmp_path)

    manifest = run_prepared(prepare_run(tmp_path / "runs", tmp_path.name, *pinned_stages(tmp_path)))

    assert manifest["status"] == "awaiting_review"
    assert set(manifest["halted_at"]) == {"review_a", "review_b"}
    assert _stage_status(manifest, "review_a") == "awaiting_review"
    assert _stage_status(manifest, "review_b") == "awaiting_review"
    assert _stage_status(manifest, "tail_a") == "pending"
    assert _stage_status(manifest, "tail_b") == "pending"


def test_halted_queue_stages_item_counts_reach_the_run_manifest(tmp_path):
    """On the halt path the raise, not a returned frame, carries the counts into the manifest."""
    _write_stage(tmp_path, "01_load.json", _five_item_load_stage(tmp_path))
    _write_stage(tmp_path, "02_review.json",
                 _filtered_queue_stage("review", "load", "val > 3"))
    _seed_version(tmp_path)

    manifest = run_prepared(prepare_run(tmp_path / "runs", tmp_path.name, *pinned_stages(tmp_path)))

    assert manifest["status"] == "awaiting_review"
    assert _stage_status(manifest, "review") == "awaiting_review"
    assert manifest["human_review_queue_stats"] == {
        "review": {
            "items_queued_total": 2, "items_passed_through": 3,
            "items_pending": 2, "items_decided": 0,
        }
    }

    # The same counts survive the round trip to disk — the run page reads them
    # back from the stored manifest, not from the in-memory object.
    on_disk = read_manifest(tmp_path, manifest["run_id"])
    assert on_disk["human_review_queue_stats"] == manifest["human_review_queue_stats"]


def test_multi_halt_run_renders_the_full_halted_at_list_through_the_web_layer(
    tmp_path, monkeypatch
):
    workspace.set_projects_dir(tmp_path)
    project_dir = tmp_path / "multi_halt_web"
    _write_stage(project_dir, "01_load.json", _load_items_stage(project_dir))
    _write_stage(project_dir, "02_review_a.json", _queue_stage("review_a", "load"))
    _write_stage(project_dir, "03_review_b.json", _queue_stage("review_b", "load"))
    _seed_version(project_dir)

    manifest = run_prepared(prepare_run(project_dir / "runs", project_dir.name, *pinned_stages(project_dir)))
    run_id = manifest["run_id"]

    client = TestClient(app)

    status = client.get(f"/project/multi_halt_web/runs/{run_id}/status").json()
    assert set(status["halted_at"]) == {"review_a", "review_b"}
    assert status["terminal"] is True  # awaiting_review stops the live poller

    page = client.get(f"/project/multi_halt_web/runs/{run_id}")
    assert page.status_code == 200
    assert "queue/review_a" in page.text
    assert "queue/review_b" in page.text


# ── Legacy scalar halted_at manifests ────────────────────────────────────────

def test_legacy_scalar_halted_at_manifest_renders_one_queue_link(tmp_path, monkeypatch):
    workspace.set_projects_dir(tmp_path)
    project_dir = tmp_path / "legacy_halt"
    _write_stage(project_dir, "01_load.json", _load_items_stage(project_dir))
    _write_stage(project_dir, "02_review.json", _queue_stage("review", "load"))
    _seed_version(project_dir)

    halted = run_prepared(prepare_run(project_dir / "runs", project_dir.name, *pinned_stages(project_dir)))
    run_id = halted["run_id"]

    # Rewrite the on-disk manifest to the legacy scalar shape.
    manifest_project = project_dir

    manifest_run = run_id
    on_disk = read_manifest(manifest_project, manifest_run)
    on_disk["halted_at"] = "review"
    store_manifest(manifest_project, manifest_run, on_disk)

    normalized = loading.load_manifest(project_dir.name, run_id)
    assert normalized["halted_at"] == ["review"]

    client = TestClient(app)
    page = client.get(f"/project/legacy_halt/runs/{run_id}")
    assert page.status_code == 200
    # One review-queue link for the whole "review" id — not one per character
    # ("queue/r", "queue/e", ...). The match runs through the href's closing
    # quote so a queue-file path ("queue/review.parquet") cannot count as one.
    assert page.text.count('queue/review"') == 1
    assert 'queue/r"' not in page.text


def test_manifest_paths_are_posix_on_every_platform(tmp_path, monkeypatch):
    workspace.set_projects_dir(tmp_path)
    project_dir = tmp_path / "posix_paths"
    _write_stage(project_dir, "01_load.json", _load_items_stage(project_dir))
    _write_stage(project_dir, "02_review.json", _queue_stage("review", "load"))
    _seed_version(project_dir)

    halted = run_prepared(prepare_run(project_dir / "runs", project_dir.name, *pinned_stages(project_dir)))

    manifest_project = project_dir


    manifest_run = halted["run_id"]
    manifest = read_manifest(manifest_project, manifest_run)
    persisted = {
        (record["stage_id"], key): record[key]
        for record in manifest["stage_records"]
        for key in ("output_path", "queue_path")
        if key in record
    }
    assert persisted  # the run produced at least one persisted path
    for (stage_id, key), value in persisted.items():
        assert "\\" not in value, f"{stage_id}.{key} is not POSIX: {value!r}"


# ── Resume clears the stale halt marker ──────────────────────────────────────

def test_resume_pops_stale_halted_at_before_re_executing(tmp_path, monkeypatch):
    """Else a mid-run flush carries the marker and the run page shows the review banner."""
    _write_stage(tmp_path, "01_load.json", _load_items_stage(tmp_path))
    _write_stage(tmp_path, "02_review.json", _queue_stage("review", "load"))
    _seed_version(tmp_path)

    halted = run_prepared(prepare_run(tmp_path / "runs", tmp_path.name, *pinned_stages(tmp_path)))
    assert halted["halted_at"] == ["review"]  # the halted run recorded the marker

    captured: dict[str, bool] = {}
    real_execute = runner._execute_stages

    def capture(ordered, ctx, manifest, run_dir, outputs_so_far):
        captured["halted_at_present"] = "halted_at" in manifest
        return real_execute(ordered, ctx, manifest, run_dir, outputs_so_far)

    monkeypatch.setattr(runner, "_execute_stages", capture)
    runner.resume_run(tmp_path / "runs" / halted["run_id"], tmp_path.name, halted["run_id"],
                      *resumed_stages(tmp_path, halted["run_id"]))

    assert captured["halted_at_present"] is False


# ── Mixed error + halt ───────────────────────────────────────────────────────

def test_error_and_halt_together_report_errors_but_keep_stage_awaiting_review(tmp_path):
    _write_stage(tmp_path, "01_load.json", _load_items_stage(tmp_path))
    _write_stage(tmp_path, "02_boom.json", _raising_stage("boom", "load"))
    _write_stage(tmp_path, "03_review.json", _queue_stage("review", "load"))
    _seed_version(tmp_path)

    manifest = run_prepared(prepare_run(tmp_path / "runs", tmp_path.name, *pinned_stages(tmp_path)))

    assert manifest["status"] == "errors"
    assert _stage_status(manifest, "boom") == "error"
    assert _stage_status(manifest, "review") == "awaiting_review"
    assert manifest["halted_at"] == ["review"]


# ── Cancel after a halt ──────────────────────────────────────────────────────

def test_cancel_after_a_halt_clears_halted_at_and_reports_cancelled(tmp_path, monkeypatch):
    _write_stage(tmp_path, "01_load.json", _load_items_stage(tmp_path))
    _write_stage(tmp_path, "02_review.json", _queue_stage("review", "load"))
    _write_stage(tmp_path, "03_good.json", _passthrough_stage("good_tail", "load"))
    _seed_version(tmp_path)

    calls = {"n": 0}

    def fake_consume_cancel(project: str, run_id: str) -> bool:
        calls["n"] += 1
        return calls["n"] > 2  # nothing at load's/review's checkpoints, then a message

    monkeypatch.setattr(executor, "consume_cancel", fake_consume_cancel)

    manifest = run_prepared(prepare_run(tmp_path / "runs", tmp_path.name, *pinned_stages(tmp_path)))

    assert manifest["status"] == "cancelled"
    assert manifest["cancelled_at"] == "good_tail"
    assert "halted_at" not in manifest
    assert _stage_status(manifest, "load") == "ok"
    assert _stage_status(manifest, "review") == "awaiting_review"
    assert _stage_status(manifest, "good_tail") == "pending"

    on_disk = read_manifest(tmp_path, manifest["run_id"])
    assert on_disk["status"] == "cancelled"
    assert "halted_at" not in on_disk


# ── Resume after error is not stale ──────────────────────────────────────────

def test_row_error_stage_blocks_downstream_and_resume_is_not_stale(tmp_path, monkeypatch):
    failing = {"text": "x"}  # `text` is the one column the signature reads

    def fake_call_llm(stage_id, llm_config, row, **kwargs):
        if row["text"] == failing["text"]:
            raise RuntimeError("boom")
        return {"score": 5}

    monkeypatch.setattr(lt, "call_llm", fake_call_llm)

    _write_stage(tmp_path, "01_load.json", _score_load_stage(tmp_path))
    _write_stage(tmp_path, "02_score.json", _score_stage("score", "load"))
    _write_stage(tmp_path, "03_tail.json",
                 _passthrough_stage("tail", "score", schema=_SCORED_SCHEMA))
    _write_stage(tmp_path, "04_good.json",
                 _passthrough_stage("good_tail", "load", schema=_ID_TEXT_SCHEMA))
    _seed_version(tmp_path)

    first = run_prepared(prepare_run(tmp_path / "runs", tmp_path.name, *pinned_stages(tmp_path)))

    assert first["status"] == "errors"
    assert _stage_status(first, "score") == "error"
    assert _stage_status(first, "tail") == "pending"
    assert _stage_status(first, "good_tail") == "ok"

    outputs = tmp_path / "runs" / first["run_id"] / "outputs"
    assert (outputs / "good_tail.parquet").exists()
    assert not (outputs / "tail.parquet").exists()

    # Remove the failure and resume the same run: score re-runs (both rows now
    # succeed), and tail runs on score's real output rather than a stale frame.
    failing["text"] = None
    resumed = runner.resume_run(tmp_path / "runs" / first["run_id"], tmp_path.name, first["run_id"],
                            *resumed_stages(tmp_path, first["run_id"]))

    assert resumed["status"] == "ok"
    assert _stage_status(resumed, "score") == "ok"
    assert _stage_status(resumed, "tail") == "ok"
    assert _stage_status(resumed, "good_tail") == "ok"
    tail_out = pd.read_parquet(outputs / "tail.parquet")
    assert list(tail_out["score"]) == [5, 5]  # real generated data, not stale


def test_resume_after_error_reruns_the_errored_stage_and_its_downstream(tmp_path):
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    csv_path = tmp_path / "data" / "items.csv"
    pd.DataFrame({"id": ["a", "b"], "val": [1, 2]}).to_csv(csv_path, index=False)
    load = {"id": "load", "description": "Load", "type": "input_data",
            "connector": {"kind": "file", "params": {"path": str(csv_path), "format": "csv"}},
            "signature": {"form": "replaces", "produces": _ID_VAL_COLUMNS}}
    _write_stage(tmp_path, "01_load.json", load)
    _write_stage(tmp_path, "02_mid.json", _passthrough_stage("mid", "load"))
    _write_stage(tmp_path, "03_tail.json", _passthrough_stage("tail", "mid"))
    _seed_version(tmp_path)

    prep = prepare_run(tmp_path / "runs", tmp_path.name, *pinned_stages(tmp_path))  # preflight hashes the valid file
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
    resumed = runner.resume_run(tmp_path / "runs" / first["run_id"], tmp_path.name, first["run_id"],
                            *resumed_stages(tmp_path, first["run_id"]))

    assert resumed["status"] == "ok"
    assert _stage_status(resumed, "load") == "ok"
    assert _stage_status(resumed, "mid") == "ok"
    assert _stage_status(resumed, "tail") == "ok"
    tail_out = pd.read_parquet(outputs / "tail.parquet")
    assert list(tail_out["val"]) == [1, 2]  # real data, not a stale empty frame


# ── A resume executes the version its run pinned, and nothing else ───────────

def test_resume_refuses_stages_belonging_to_another_version(tmp_path):
    workspace.set_projects_dir(tmp_path)
    project_dir = tmp_path / "wrong_version"
    _write_stage(project_dir, "01_load.json", _load_items_stage(project_dir))
    _write_stage(project_dir, "02_review.json", _queue_stage("review", "load"))
    _seed_version(project_dir)

    halted = run_prepared(prepare_run(project_dir / "runs", project_dir.name, *pinned_stages(project_dir)))
    stages, _ = resumed_stages(project_dir, halted["run_id"])

    with pytest.raises(ValueError, match="pinned to workflow version"):
        runner.resume_run(project_dir / "runs" / halted["run_id"], project_dir.name,
                          halted["run_id"], stages, "20990101T000000")


def test_resume_reads_the_pinned_version_not_the_working_copy(tmp_path):
    workspace.set_projects_dir(tmp_path)
    project_dir = tmp_path / "drifted_copy"
    _write_stage(project_dir, "01_load.json", _load_items_stage(project_dir))
    _write_stage(project_dir, "02_review.json", _queue_stage("review", "load"))
    _seed_version(project_dir)

    halted = run_prepared(prepare_run(project_dir / "runs", project_dir.name, *pinned_stages(project_dir)))
    assert halted["status"] == "awaiting_review"

    # Break the working copy AFTER the run pinned its version. The pinned
    # snapshot is untouched, so the resume must still find loadable stages.
    _write_stage(project_dir, "01_load.json", {"id": "load", "type": "input_data"})
    with pytest.raises(WorkflowLoadError):
        load_workflow(project_dir.name)

    workflow, workflow_version = resumed_stages(project_dir, halted["run_id"])
    assert [s.id for s in workflow.stages] == ["load", "review"]
    assert workflow_version == halted["workflow_version"]

    response = TestClient(app).post(
        f"/project/drifted_copy/runs/{halted['run_id']}/resume", follow_redirects=False)
    assert response.status_code == 303


def test_resume_of_a_run_with_no_pinned_version_fails_loudly(tmp_path):
    workspace.set_projects_dir(tmp_path)
    project_dir = tmp_path / "unpinned"
    _write_stage(project_dir, "01_load.json", _load_items_stage(project_dir))
    _write_stage(project_dir, "02_review.json", _queue_stage("review", "load"))
    _seed_version(project_dir)

    halted = run_prepared(prepare_run(project_dir / "runs", project_dir.name, *pinned_stages(project_dir)))
    manifest_project = project_dir

    manifest_run = halted["run_id"]
    manifest = read_manifest(manifest_project, manifest_run)
    manifest["workflow_version"] = None
    store_manifest(manifest_project, manifest_run, manifest)

    with pytest.raises(RunVersionUnresolvableError, match="records no workflow version"):
        run_service.read_pinned_version("unpinned", halted["run_id"])

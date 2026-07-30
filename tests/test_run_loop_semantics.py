from __future__ import annotations

import json

import pandas as pd
from fastapi.testclient import TestClient

import app.runtime.executor as executor
import app.runtime.runner as runner
import app.web.loading as loading
from app.main import app
from app.runtime.runner import prepare_run, run_prepared
from app.runtime.stages import llm_transform as lt
from app.services import versioning
from app.services.versioning import create_version_from_disk
from app.services import workspace


# The three frame shapes this file's DAGs carry. Declared once so an upstream's
# output_schema and its downstream's input `schema` cannot drift apart.
_ID_VAL_SCHEMA = {"columns": [{"name": "id", "type": "str"},
                              {"name": "val", "type": "int"}],
                  "primary_key": ["id"]}
_ID_TEXT_SCHEMA = {"columns": [{"name": "id", "type": "str"},
                               {"name": "text", "type": "str"}],
                   "primary_key": ["id"]}
_SCORED_SCHEMA = {"columns": [{"name": "id", "type": "str"},
                              {"name": "text", "type": "str"},
                              {"name": "score", "type": "int", "nullable": False}],
                  "primary_key": ["id"]}


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
                          "params": {"path": str(csv_path), "format": "csv"}},
            "output_schema": _ID_VAL_SCHEMA}


def _raising_stage(stage_id, input_id, name="Boom", schema=_ID_VAL_SCHEMA):
    """A python_frame_function whose transform raises — the stage errors. It
    emits nothing, so `schema` (its input's shape) stands as its declared output
    too: the identity shape it would have emitted had it not raised."""
    return {"id": stage_id, "name": name, "type": "python_frame_function",
            "inputs": [{"id": input_id, "schema": schema}],
            "output_schema": schema,
            "function": {"kind": "inline",
                         "code": "def transform(df):\n    raise ValueError('boom')\n"}}


def _passthrough_stage(stage_id, input_id, name="Passthrough", schema=_ID_VAL_SCHEMA):
    """An identity python_frame_function: `schema` is both the shape it expects
    from `input_id` and the shape it emits."""
    return {"id": stage_id, "name": name, "type": "python_frame_function",
            "inputs": [{"id": input_id, "schema": schema}],
            "output_schema": schema,
            "function": {"kind": "inline",
                         "code": "def transform(df):\n    return df\n"}}


def _score_load_stage(root):
    """An input_data stage reading a 2-row (id, text) csv for an llm_transform."""
    (root / "data").mkdir(parents=True, exist_ok=True)
    csv_path = root / "data" / "score_items.csv"
    pd.DataFrame({"id": ["a", "b"], "text": ["x", "y"]}).to_csv(csv_path, index=False)
    return {"id": "load", "name": "Load", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": str(csv_path), "format": "csv"}},
            "output_schema": _ID_TEXT_SCHEMA}


def _score_stage(stage_id, input_id, name="Score"):
    """An llm_transform adding a non-null `score` column to each (id, text) row."""
    return {"id": stage_id, "name": name, "type": "llm_transform",
            "inputs": [{"id": input_id, "schema": _ID_TEXT_SCHEMA}],
            "output_schema": _SCORED_SCHEMA,
            "llm": {"prompt_template": "Rate: {text}"}}


def _queue_stage(stage_id, input_id, name="Review"):
    """A human_review_queue with no cached decisions yet — it halts. Its
    output_schema keeps the (id, val) columns a reviewed row carries through;
    the stage projects onto exactly what it declares, so the reviewer
    bookkeeping columns are not part of its output."""
    return {"id": stage_id, "name": name, "type": "human_review_queue",
            "inputs": [{"id": input_id, "schema": _ID_VAL_SCHEMA}],
            "output_schema": _ID_VAL_SCHEMA,
            "queue": {}}


def _five_item_load_stage(root):
    """An input_data stage reading 5 rows whose `val` runs 1..5, so a queue
    filter can split them into unequal partitions."""
    (root / "data").mkdir(parents=True, exist_ok=True)
    csv_path = root / "data" / "five_items.csv"
    pd.DataFrame({"id": list("abcde"), "val": [1, 2, 3, 4, 5]}).to_csv(csv_path, index=False)
    return {"id": "load", "name": "Load", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": str(csv_path), "format": "csv"}},
            "output_schema": _ID_VAL_SCHEMA}


def _filtered_queue_stage(stage_id, input_id, flt, name="Review"):
    """A human_review_queue that reviews only the rows `flt` selects. With no
    cached decisions, every selected row is pending — so it halts. Declares the
    same (id, val) output as `_queue_stage`."""
    return {"id": stage_id, "name": name, "type": "human_review_queue",
            "inputs": [{"id": input_id, "schema": _ID_VAL_SCHEMA}],
            "output_schema": _ID_VAL_SCHEMA,
            "queue": {"filter": flt}}


def _stage_status(manifest, stage_id):
    for record in manifest["stage_records"]:
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


def test_halted_queue_stages_item_counts_reach_the_run_manifest(tmp_path):
    """A halting queue stage's item counts land in the run manifest, with the
    numbers the map actually produced.

    The counts are accumulated on the mapper and travel out on the stage's
    StageContribution — and on the halt path the raise, not a returned frame,
    is what carries it, so the executor folds `HaltForReview.contribution` into
    the manifest. That fold is the only thing standing between the counters and
    the run page; asserting the key exists would not catch a fold that zeroed
    them, so the exact numbers are asserted.

    5 rows in, filter `val > 3`: 2 rows are subject to review and 3 pass
    through. No decision is cached, so both reviewable rows are pending and
    none is decided."""
    _write_stage(tmp_path, "01_load.json", _five_item_load_stage(tmp_path))
    _write_stage(tmp_path, "02_review.json",
                 _filtered_queue_stage("review", "load", "val > 3"))
    _seed_version(tmp_path)

    manifest = run_prepared(prepare_run(tmp_path, repo_root=tmp_path))

    assert manifest["status"] == "awaiting_review"
    assert _stage_status(manifest, "review") == "awaiting_review"
    assert manifest["human_review_queue_stats"] == {
        "review": {
            "items_queued_total": 2, "items_passed_through": 3,
            "items_pending": 2, "items_decided": 0,
        }
    }

    # The same counts survive the round trip to disk — the run page reads them
    # back from manifest.json, not from the in-memory object.
    on_disk = json.loads(
        (tmp_path / "runs" / manifest["run_id"] / "manifest.json").read_text(encoding="utf-8")
    )
    assert on_disk["human_review_queue_stats"] == manifest["human_review_queue_stats"]


def test_multi_halt_run_renders_the_full_halted_at_list_through_the_web_layer(
    tmp_path, monkeypatch
):
    """Verifies the `/status` JSON poller and run_detail.html against a real
    2-halt run — Part A's status consumers must render the whole `halted_at`
    list, not just the first (or a single scalar id, the pre-fork-aware
    shape)."""
    workspace.set_projects_dir(tmp_path)
    project_dir = tmp_path / "multi_halt_web"
    _write_stage(project_dir, "01_load.json", _load_items_stage(project_dir))
    _write_stage(project_dir, "02_review_a.json", _queue_stage("review_a", "load"))
    _write_stage(project_dir, "03_review_b.json", _queue_stage("review_b", "load"))
    _seed_version(project_dir)

    manifest = run_prepared(prepare_run(project_dir, repo_root=project_dir))
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
    """A pre-fork-aware manifest persisted `halted_at` as a scalar stage-id
    string. load_manifest normalizes it to a one-element list so run_detail.html
    renders a single review-queue link, not one per character (a `{% for %}`
    over a string iterates characters)."""
    workspace.set_projects_dir(tmp_path)
    project_dir = tmp_path / "legacy_halt"
    _write_stage(project_dir, "01_load.json", _load_items_stage(project_dir))
    _write_stage(project_dir, "02_review.json", _queue_stage("review", "load"))
    _seed_version(project_dir)

    halted = run_prepared(prepare_run(project_dir, repo_root=project_dir))
    run_id = halted["run_id"]

    # Rewrite the on-disk manifest to the legacy scalar shape.
    manifest_path = project_dir / "runs" / run_id / "manifest.json"
    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    on_disk["halted_at"] = "review"
    manifest_path.write_text(json.dumps(on_disk), encoding="utf-8")

    normalized = loading.load_manifest(project_dir / "runs" / run_id)
    assert normalized["halted_at"] == ["review"]

    client = TestClient(app)
    page = client.get(f"/project/legacy_halt/runs/{run_id}")
    assert page.status_code == 200
    # One review-queue link for the whole "review" id — not one per character
    # ("queue/r", "queue/e", ...). Match through the href's closing quote:
    # the page also embeds the raw manifest JSON, whose POSIX queue-file path
    # ("queue/review.parquet") would otherwise add a false match.
    assert page.text.count('queue/review"') == 1
    assert 'queue/r"' not in page.text


def test_manifest_paths_are_posix_on_every_platform(tmp_path, monkeypatch):
    """`output_path` and `queue_path` are persisted POSIX-style: the manifest
    is a portable JSON record, so identical runs must serialize identically
    regardless of the OS's native path separator."""
    workspace.set_projects_dir(tmp_path)
    project_dir = tmp_path / "posix_paths"
    _write_stage(project_dir, "01_load.json", _load_items_stage(project_dir))
    _write_stage(project_dir, "02_review.json", _queue_stage("review", "load"))
    _seed_version(project_dir)

    halted = run_prepared(prepare_run(project_dir, repo_root=project_dir))

    manifest_path = project_dir / "runs" / halted["run_id"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
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
    """A resumed run is no longer halted, so resume_run must hand _execute_stages
    a manifest WITHOUT `halted_at` — otherwise a mid-run flush (which persists
    status `running`) would carry the halt marker and the run page would show the
    review banner + queue links while the halted stage re-runs. The loop re-adds
    `halted_at` only if a stage halts again."""
    _write_stage(tmp_path, "01_load.json", _load_items_stage(tmp_path))
    _write_stage(tmp_path, "02_review.json", _queue_stage("review", "load"))
    _seed_version(tmp_path)

    halted = run_prepared(prepare_run(tmp_path, repo_root=tmp_path))
    assert halted["halted_at"] == ["review"]  # the halted run recorded the marker

    captured: dict[str, bool] = {}
    real_execute = runner._execute_stages

    def capture(ordered, ctx, manifest, run_dir, outputs_so_far):
        captured["halted_at_present"] = "halted_at" in manifest
        return real_execute(ordered, ctx, manifest, run_dir, outputs_so_far)

    monkeypatch.setattr(runner, "_execute_stages", capture)
    runner.resume_run(tmp_path, halted["run_id"], tmp_path)

    assert captured["halted_at_present"] is False


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


# ── Cancel after a halt ──────────────────────────────────────────────────────

def test_cancel_after_a_halt_clears_halted_at_and_reports_cancelled(tmp_path, monkeypatch):
    """load -> review (halts, `continue`s to the next stage) -> good_tail. A
    cancel is consumed at good_tail's between-stage checkpoint, AFTER review
    already halted. Cancel is a hard stop that wins over an earlier halt: the
    manifest must not carry a leftover `halted_at` (which would make
    run_detail.html show the halt review banner on a cancelled run) — mirrors
    the pre-fork-aware runner, whose cancelled branch always popped
    `halted_at`.

    consume_cancel is monkeypatched deterministically (same technique as
    test_run_cancel.py's mid-run cancel test) so the cancel lands on the third
    between-stage checkpoint (good_tail's) rather than depending on thread
    timing."""
    _write_stage(tmp_path, "01_load.json", _load_items_stage(tmp_path))
    _write_stage(tmp_path, "02_review.json", _queue_stage("review", "load"))
    _write_stage(tmp_path, "03_good.json", _passthrough_stage("good_tail", "load"))
    _seed_version(tmp_path)

    calls = {"n": 0}

    def fake_consume_cancel(project: str, run_id: str) -> bool:
        calls["n"] += 1
        return calls["n"] > 2  # nothing at load's/review's checkpoints, then a message

    monkeypatch.setattr(executor, "consume_cancel", fake_consume_cancel)

    manifest = run_prepared(prepare_run(tmp_path, repo_root=tmp_path))

    assert manifest["status"] == "cancelled"
    assert manifest["cancelled_at"] == "good_tail"
    assert "halted_at" not in manifest
    assert _stage_status(manifest, "load") == "ok"
    assert _stage_status(manifest, "review") == "awaiting_review"
    assert _stage_status(manifest, "good_tail") == "pending"

    on_disk = json.loads(
        (tmp_path / "runs" / manifest["run_id"] / "manifest.json").read_text(encoding="utf-8")
    )
    assert on_disk["status"] == "cancelled"
    assert "halted_at" not in on_disk


# ── Resume after error is not stale ──────────────────────────────────────────

def test_row_error_stage_blocks_downstream_and_resume_is_not_stale(tmp_path, monkeypatch):
    """load -> score (llm, one row fails generation) -> tail, plus an independent
    load -> good_tail fork. A per-row generation failure marks `score` `error`,
    so it MUST block its downstream exactly like a raised error: `tail` stays
    pending with no output file (never run on `score`'s partial frame and marked
    `ok`), while the independent `good_tail` fork finishes. On resume with the
    failure removed, `score` re-runs and `tail` runs on its real (non-stale)
    output. This is the row-error path of the fabricated-success/stale-reuse bug
    the fork-blocking invariant closes."""
    failing = {"id": "a"}  # which input id's generation fails; cleared before resume

    def fake_call_llm(stage_id, llm_config, row, **kwargs):
        if row["id"] == failing["id"]:
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

    first = run_prepared(prepare_run(tmp_path, repo_root=tmp_path))

    assert first["status"] == "errors"
    assert _stage_status(first, "score") == "error"
    assert _stage_status(first, "tail") == "pending"
    assert _stage_status(first, "good_tail") == "ok"

    outputs = tmp_path / "runs" / first["run_id"] / "outputs"
    assert (outputs / "good_tail.parquet").exists()
    assert not (outputs / "tail.parquet").exists()

    # Remove the failure and resume the same run: score re-runs (both rows now
    # succeed), and tail runs on score's real output rather than a stale frame.
    failing["id"] = None
    resumed = runner.resume_run(tmp_path, first["run_id"], tmp_path)

    assert resumed["status"] == "ok"
    assert _stage_status(resumed, "score") == "ok"
    assert _stage_status(resumed, "tail") == "ok"
    assert _stage_status(resumed, "good_tail") == "ok"
    tail_out = pd.read_parquet(outputs / "tail.parquet")
    assert list(tail_out["score"]) == [5, 5]  # real generated data, not stale


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
            "connector": {"kind": "file", "params": {"path": str(csv_path), "format": "csv"}},
            "output_schema": _ID_VAL_SCHEMA}
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

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
import sys
import time

import pandas as pd
import pytest

import app.runtime.runner as runner
from app.core.errors import NoVersionToRunError, SubsetRunError
from app.core.run_status import RunStatus
from app.models import Stage, Workflow
from app.runtime.runner import execute_run, resume_run
from app.runtime.executor import _raise_if_run_failed, run_subset
from app.runtime.manifest import RunManifest
from app.runtime.stages import llm_transform as lt
from app.services.loader import WorkflowLoadError
from app.services import versioning
from app.services.versioning import create_version_from_disk, list_versions


def _seed_version(root):
    """Create the initial version a run targets, and PUBLISH it. Runs no longer
    create versions, so a test that builds a working copy must snapshot it into
    a version before running against it; runs are also gated on published, so
    the seed must be published for a run against it to succeed."""
    vid = create_version_from_disk(root, message="test seed", reviewer="test").version_id
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
        "limit": 2,
    }
    (root / "compiled" / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")


def test_limit_truncates_and_is_recorded(tmp_path):
    _make_project(tmp_path)
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, repo_root=tmp_path)

    assert manifest["status"] == "ok"
    [rec] = manifest["stage_records"]
    assert rec["status"] == "ok"
    assert rec["output_row_count"] == 2                                   # truncated from 5
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
    manifest = execute_run(tmp_path, repo_root=tmp_path, bust_cache=True)

    assert manifest["bust_cache"] is True
    on_disk = json.loads(
        (tmp_path / "runs" / manifest["run_id"] / "manifest.json")
        .read_text(encoding="utf-8"))
    assert on_disk["bust_cache"] is True


def test_an_ordinary_run_records_bust_cache_false(tmp_path):
    _make_project(tmp_path)
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, repo_root=tmp_path)
    assert manifest["bust_cache"] is False


def test_cli_bust_cache_flag_reaches_execute_run(tmp_path, monkeypatch):
    """`python -m app.runtime.runner <project> --bust-cache` threads the flag
    into the run; without it the run is not busted."""
    calls: list[bool] = []

    def fake_execute_run(project_dir, repo_root, version_id=None, limits=None,
                         offsets=None, bindings=None, bust_cache=False):
        calls.append(bust_cache)
        return {"run_id": "r", "workflow_version": "v", "status": RunStatus.OK,
                "stage_records": []}

    monkeypatch.setattr(runner, "execute_run", fake_execute_run)
    monkeypatch.setattr(sys, "argv", ["runner", str(tmp_path), "--bust-cache"])
    assert runner.main() == 0
    monkeypatch.setattr(sys, "argv", ["runner", str(tmp_path)])
    assert runner.main() == 0
    assert calls == [True, False]


def test_cli_rejects_an_unknown_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["runner", str(tmp_path), "--nope"])
    assert runner.main() == 1


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
                      "params": {"path": str(root / "data" / "items.csv"), "format": "csv"}},
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
    manifest = execute_run(tmp_path, repo_root=tmp_path)
    assert manifest["status"] == "ok"
    records = {r["stage_id"]: r for r in manifest["stage_records"]}
    assert records["consume"]["status"] == "ok"
    assert records["consume"]["output_row_count"] == 2


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
    manifest = execute_run(tmp_path, repo_root=tmp_path)

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
    load = Stage.model_validate({
        "id": "load", "name": "Load items", "type": "input_data",
        "connector": {"kind": "file"},
    })
    score = Stage.model_validate({
        "id": "score", "name": "Score items", "type": "llm_transform",
        "inputs": [{"id": "load", "schema": {
            "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}],
            "primary_key": ["id"]}}],
        "output_schema": {
            "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"},
                        {"name": "score", "type": "int", "nullable": False}],
            "primary_key": ["id"]},
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
    load = Stage.model_validate({
        "id": "load", "name": "Load items", "type": "input_data",
        "connector": {"kind": "file"},
    })
    clean = Stage.model_validate({
        "id": "clean", "name": "Clean rows", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": {
            "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}]}}],
        "output_schema": {
            "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}]},
        "function": {"kind": "inline", "code": "def transform(row): return row"},
    })
    boom = Stage.model_validate({
        "id": "score", "name": "Score rows", "type": "python_row_function",
        "inputs": [{"id": "clean", "schema": {
            "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}]}}],
        "output_schema": {
            "columns": [{"name": "id", "type": "str"}, {"name": "score", "type": "int"}]},
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
        execute_run(tmp_path, repo_root=tmp_path)
    assert not (tmp_path / "runs").exists()
    assert list_versions(tmp_path) == []


def test_unpublished_latest_is_skipped_for_an_older_published_version(tmp_path):
    """A run given no explicit version_id pins to the newest PUBLISHED version,
    not merely the newest version — an unpublished draft snapshot never leaks
    into a run just because it is more recent."""
    _make_project(tmp_path)
    published_id = _seed_version(tmp_path)  # published

    time.sleep(1)  # version ids are second-resolution
    create_version_from_disk(tmp_path, message="unpublished newer", reviewer="test")

    manifest = execute_run(tmp_path, repo_root=tmp_path)
    assert manifest["workflow_version"] == published_id
    assert manifest["status"] == "ok"


def test_run_with_no_published_version_fails_loudly(tmp_path):
    """A version exists but nothing is published yet: a run still refuses,
    just like the no-version-at-all case, rather than silently running an
    unreviewed snapshot."""
    _make_project(tmp_path)
    create_version_from_disk(tmp_path, message="unpublished", reviewer="test")

    with pytest.raises(NoVersionToRunError):
        execute_run(tmp_path, repo_root=tmp_path)
    assert not (tmp_path / "runs").exists()


def test_run_with_explicit_unpublished_id_fails_loudly(tmp_path):
    """An explicit version_id naming a real but unpublished version is refused
    the same way — a run pins to a published version, never to a draft."""
    _make_project(tmp_path)
    unpublished_id = create_version_from_disk(tmp_path, message="unpublished", reviewer="test").version_id

    with pytest.raises(NoVersionToRunError):
        execute_run(tmp_path, repo_root=tmp_path, version_id=unpublished_id)
    assert not (tmp_path / "runs").exists()


def test_create_version_rejects_invalid_working_copy(tmp_path):
    """create_version_from_disk strict-loads before it snapshots: an invalid
    working copy raises WorkflowLoadError and writes NOTHING, so no invalid
    workflow can be immortalised as a version."""
    (tmp_path / "compiled").mkdir(parents=True)
    bad = {"id": "load", "name": "Load", "type": "input_data",
           "connector": {"kind": "file",
                         "params": {"path": "data/items.csv", "format": "csv"}}}  # relative path
    (tmp_path / "compiled" / "01_load.json").write_text(
        json.dumps(bad), encoding="utf-8")

    with pytest.raises(WorkflowLoadError) as exc:
        create_version_from_disk(tmp_path, message="x", reviewer="test")
    assert any("params.path" in i for i in exc.value.issues)
    assert list_versions(tmp_path) == []  # snapshotted nothing


def test_invalid_workflow_never_becomes_a_version_and_run_never_pins_stale(tmp_path):
    """Regression for the version-lifecycle bug: a run used to snapshot the
    working copy as a version BEFORE validating it, so an invalid workflow got
    immortalised as 'the latest' and every later default run reloaded that
    poisoned snapshot and failed with a stale error. Now runs never create
    versions and create_version_from_disk validates first, so the bug is
    impossible."""
    # Invalid working copy: file connector params.path is relative, not absolute.
    (tmp_path / "compiled").mkdir(parents=True)
    bad = {"id": "load", "name": "Load", "type": "input_data",
           "connector": {"kind": "file",
                         "params": {"path": "data/items.csv", "format": "csv"}}}
    (tmp_path / "compiled" / "01_load.json").write_text(
        json.dumps(bad), encoding="utf-8")

    # You cannot make a version from it, and it writes nothing.
    with pytest.raises(WorkflowLoadError):
        create_version_from_disk(tmp_path, message="x", reviewer="test")
    assert list_versions(tmp_path) == []

    # A run refuses (no version) and does NOT auto-create one — nothing on disk.
    with pytest.raises(NoVersionToRunError):
        execute_run(tmp_path, repo_root=tmp_path)
    assert list_versions(tmp_path) == []
    assert not (tmp_path / "runs").exists()

    # Fix the working copy. A run STILL refuses until a version is created
    # explicitly — it never silently pins to a stale snapshot (there is none).
    (tmp_path / "data").mkdir(parents=True)
    pd.DataFrame({"name": ["a"], "val": [1]}).to_csv(
        tmp_path / "data" / "items.csv", index=False)
    good = {"id": "load", "name": "Load", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": str(tmp_path / "data" / "items.csv"), "format": "csv"}}}
    (tmp_path / "compiled" / "01_load.json").write_text(
        json.dumps(good), encoding="utf-8")
    with pytest.raises(NoVersionToRunError):
        execute_run(tmp_path, repo_root=tmp_path)

    # Explicit creation, then the run succeeds against that version.
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, repo_root=tmp_path)
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
              "connector": {"kind": "file", "params": {}}}  # no workflow-authored path
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

    result = resume_run(tmp_path, run_id, repo_root=tmp_path)

    [rec] = result["stage_records"]
    assert rec["status"] == "ok", rec.get("error")
    out = pd.read_parquet(run_dir / "outputs" / "load.parquet")
    assert list(out["name"]) == ["bound-row"]


def test_the_documented_cli_runs_a_project_with_nothing_configured(tmp_path, monkeypatch):
    """`python -m app.runtime.runner <project_dir>` is a standalone process: no
    server lifespan wired storage for it, so its own entry point must. Seeds a
    version through an on-disk store, then drops BOTH process-wide stores to
    simulate the fresh process the CLI actually runs in."""
    from app.core import frames as frames_module
    from app.core import persistence as persistence_module
    from app.core.persistence import SqliteKvStore, configure_store

    db_path = tmp_path / "db" / "app.db"
    db_path.parent.mkdir(parents=True)
    monkeypatch.setenv("CW_DB_PATH", str(db_path))
    monkeypatch.setenv("CW_FRAMES_ROOT", str(tmp_path / "frames"))

    project_dir = tmp_path / "project"
    configure_store(SqliteKvStore(str(db_path)))
    _make_project(project_dir)
    _add_frame_stage(project_dir)
    _seed_version(project_dir)

    monkeypatch.setattr(persistence_module, "_store", None)
    monkeypatch.setattr(frames_module, "_frame_store", None)
    monkeypatch.setattr(sys, "argv", ["runner", str(project_dir)])

    assert runner.main() == 0
    assert persistence_module.is_store_configured()
    assert frames_module.is_frame_store_configured()
    assert list((project_dir / "runs").iterdir())


_FRAME_STAGE_CODE = "def transform(df):\n    return df.assign(double=df['val'] * 2)\n"


def _add_frame_stage(root):
    """A python_frame_function downstream of `load` — the shape the frame cache
    intercepts, so a run of this project exercises the frame store."""
    (root / "compiled" / "02_totals.json").write_text(json.dumps({
        "id": "totals", "name": "Totals", "type": "python_frame_function",
        "inputs": [{"id": "load"}],
        "function": {"kind": "inline", "code": _FRAME_STAGE_CODE},
    }), encoding="utf-8")

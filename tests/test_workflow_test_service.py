from __future__ import annotations

import json

import pandas as pd
import pytest

from app.core.errors import (
    NoWorkflowTestSourceError,
    NoWorkflowTestVersionError,
    WorkflowTestStageScopeError,
    WorkflowTestTargetConflictError,
)
from app.models import parse_stage
from app.services import workspace
from app.services.run import load_run_stages
from app.services.workflow_test import (
    get_source_data_with_limit_and_offset,
    run_workflow_test,
)
from app.services.versioning import WorkflowVersion, list_versions, load_version
from conftest import QUEUE_COLUMNS, queue_added_columns


def _load_stage(demo):
    return {
        "id": "load", "type": "input_data", "name": "Load rows",
        "connector": {"kind": "file",
                      "params": {"path": str(demo / "data" / "rows.csv"), "format": "csv"}},
        "output_schema": {"columns": [{"name": "doc_id", "type": "str", "nullable": True},
                                      {"name": "score", "type": "int", "nullable": True}]},
    }


_LOAD_SCHEMA = {"columns": [{"name": "doc_id", "type": "str", "nullable": True},
                            {"name": "score", "type": "int", "nullable": True}]}

_CLASSIFY = {
    "id": "classify", "type": "python_row_function", "name": "Label by sign",
    "inputs": [{"id": "load", "schema": _LOAD_SCHEMA}],
    "function": {"kind": "inline", "code":
                 "def transform(row):\n"
                 "    return {'doc_id': row['doc_id'], 'score': row['score'],\n"
                 "            'label': 'pos' if row['score'] >= 0 else 'neg'}"},
    "output_schema": {"columns": [{"name": "doc_id", "type": "str", "nullable": True},
                                  {"name": "score", "type": "int", "nullable": True},
                                  {"name": "label", "type": "str", "nullable": True}]},
}

_BOOM = {
    "id": "boom", "type": "python_row_function", "name": "Always errors",
    "inputs": [{"id": "load", "schema": _LOAD_SCHEMA}],
    "function": {"kind": "inline", "code":
                 "def transform(row):\n    raise ValueError('boom')"},
    "output_schema": {"columns": [{"name": "doc_id", "type": "str", "nullable": True},
                                  {"name": "score", "type": "int", "nullable": True}]},
}

_CLASSIFY_SCHEMA = _CLASSIFY["output_schema"]

_PUBLISH = {
    "id": "publish_report", "type": "publish", "name": "Publish",
    "inputs": [{"id": "classify", "schema": _CLASSIFY_SCHEMA}],
    "function": {"kind": "inline", "code":
                 "def transform(df, output_dir):\n"
                 "    import os\n"
                 "    path = os.path.join(output_dir, 'report.json')\n"
                 "    df.to_json(path, orient='records')\n"
                 "    return df"},
    "publish": {"format": "json"},
}

# A human_review_queue whose hash resolves off the upstream primary_key.
_LOAD_PK_COLUMNS = [{"name": "doc_id", "type": "str", "nullable": True},
                    {"name": "score", "type": "int", "nullable": True}]
_LOAD_PK_SCHEMA = {"columns": _LOAD_PK_COLUMNS, "primary_key": ["doc_id"]}
_QUEUE = {
    "id": "review", "type": "human_review_queue", "name": "Review rows",
    "inputs": [{"id": "load", "schema": _LOAD_PK_SCHEMA}],
    "output_schema": {"columns": _LOAD_PK_COLUMNS + queue_added_columns(),
                      "primary_key": ["doc_id"]},
    "queue": {**QUEUE_COLUMNS, "reviewer_instructions": "check"},
}


def _seed(demo, stage_dicts, *, version_id="v1", published=False, created_at="2026-07-10T00:00:00"):
    """Save a version for the `demo` project with `stage_dicts`. Unpublished by
    default: a workflow test must work on an unpublished candidate."""
    WorkflowVersion(
        id=f"{demo.name}/{version_id}", version_id=version_id, created_at=created_at,
        message="seed", reviewer="test", published=published,
        stages=[parse_stage(s) for s in stage_dicts],
    ).save()


@pytest.fixture
def demo(tmp_path, monkeypatch):
    """A `demo` project dir with a 4-row source file bound at an absolute path,
    reachable by name through the workspace (pointed at tmp_path). The workflow-test
    service takes the project NAME `demo` and resolves it to this directory."""
    workspace.set_projects_dir(tmp_path)
    demo = tmp_path / "demo"
    (demo / "data").mkdir(parents=True)
    pd.DataFrame({"doc_id": ["a", "b", "c", "d"], "score": [1, -1, 2, -3]}).to_csv(
        demo / "data" / "rows.csv", index=False)
    return demo


def test_workflow_test_runs_frontier_over_the_slice(demo):
    """The frontier (classify) runs over the injected slice; the result is ok and
    names the executed stage."""
    _seed(demo, [_load_stage(demo), _CLASSIFY])
    result = run_workflow_test("demo")
    assert result["ok"] is True
    assert result["error"] is None
    assert result["version_id"] == "v1"
    assert result["stages_run"] == ["classify"]


def test_workflow_test_limit_and_offset_slice_the_source(demo):
    """limit/offset page the source before the frontier runs; the frontier still
    runs clean over the smaller slice."""
    _seed(demo, [_load_stage(demo), _CLASSIFY])
    result = run_workflow_test("demo", limit=2, offset=1)
    assert result["ok"] is True
    assert result["stages_run"] == ["classify"]


def test_workflow_test_writes_a_real_run_marked_is_test_run(demo):
    """A workflow test is a REAL run: its manifest lands under the project's own
    runs/<id>/ dir — the same dir a production run writes into — and carries the
    same production run-manifest fields (project + workflow_version), but
    `is_test_run` is True, the one thing marking it as a test."""
    _seed(demo, [_load_stage(demo), _CLASSIFY])
    result = run_workflow_test("demo")
    manifest_path = demo / "runs" / result["run_id"] / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["project"] == "demo"
    assert manifest["workflow_version"] == "v1"
    assert manifest["status"] == "ok"
    assert manifest["is_test_run"] is True


def test_workflow_test_runs_publish_scoped_to_its_own_run_dir(demo):
    """A publish stage RUNS in a workflow test — it is in stages_run and its
    artifact lands run-scoped under runs/<id>/, never in a project-level build
    dir."""
    _seed(demo, [_load_stage(demo), _CLASSIFY, _PUBLISH])
    result = run_workflow_test("demo")
    assert result["ok"] is True
    assert result["stages_run"] == ["classify", "publish_report"]
    run_dir = demo / "runs" / result["run_id"]
    artifacts = list(run_dir.rglob("report.json"))
    assert len(artifacts) == 1
    assert not (demo / "build").exists()


def test_workflow_test_reports_a_stage_error_as_failure(demo):
    """A frontier stage that errors makes the workflow test fail: ok False and the
    error names the offending stage."""
    _seed(demo, [_load_stage(demo), _BOOM])
    result = run_workflow_test("demo")
    assert result["ok"] is False
    assert "boom" in result["error"]
    manifest = json.loads(
        (demo / "runs" / result["run_id"] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "errors"


def test_workflow_test_auto_approves_a_queue_stage_in_memory(demo):
    """A mid-frontier human_review_queue auto-approves on a workflow test: the subset
    runs with queue_auto_approve, so every row passes straight through (ok) and
    NOTHING is written under the project's queue or decisions storage — no reviewer,
    no halt, no disk state."""
    _seed(demo, [_load_stage(demo), _QUEUE])
    result = run_workflow_test("demo")
    assert result["ok"] is True
    assert result["error"] is None
    assert result["stages_run"] == ["review"]
    # The auto-approve path never reaches for project-relative queue/decisions
    # state — those dirs must not exist after the run.
    assert not (demo / "decisions").exists()
    assert not (demo / "queue").exists()
    assert not (demo / "runs" / result["run_id"] / "queue").exists()


def test_workflow_test_raises_when_no_source_stage(demo):
    """A workflow with no input_data stage has no source to slice — raise loudly
    rather than run over an empty injection."""
    # A lone python_frame_function with no upstream input_data source: it
    # validates as a Stage on its own, and is enough to exercise the guard, which
    # runs before workflow graph validation.
    standalone = {
        "id": "standalone", "type": "python_frame_function", "name": "No source",
        "inputs": [{"id": "upstream", "schema": _LOAD_SCHEMA}],
        "output_schema": _LOAD_SCHEMA,
        "function": {"kind": "inline", "code": "def transform(df):\n    return df"},
    }
    # Build the version document directly; the guard fires before Workflow build.
    WorkflowVersion(
        id=f"{demo.name}/v1", version_id="v1", created_at="2026-07-10T00:00:00",
        message="seed", reviewer="test", published=True,
        stages=[parse_stage(standalone)],
    ).save()
    with pytest.raises(NoWorkflowTestSourceError):
        run_workflow_test("demo")


def test_workflow_test_runs_an_explicit_unpublished_version(demo):
    """A workflow test evaluates a candidate BEFORE it is published, so an explicit
    unpublished version_id runs (unlike a production run, which requires publish)."""
    _seed(demo, [_load_stage(demo), _CLASSIFY], version_id="v1", published=False)
    result = run_workflow_test("demo", version_id="v1")
    assert result["ok"] is True
    assert result["version_id"] == "v1"


def test_workflow_test_default_picks_newest_version_even_when_unpublished(demo):
    """version_id=None resolves to the newest stored version regardless of publish
    state — a newer unpublished draft wins over an older published version."""
    _seed(demo, [_load_stage(demo), _CLASSIFY],
          version_id="20260101T000000", published=True, created_at="2026-01-01T00:00:00")
    _seed(demo, [_load_stage(demo), _CLASSIFY],
          version_id="20260201T000000", published=False, created_at="2026-02-01T00:00:00")
    result = run_workflow_test("demo")
    assert result["version_id"] == "20260201T000000"


def test_workflow_test_raises_when_no_versions_exist(demo):
    """A project with no stored version has nothing to run — raise loudly,
    naming the project, rather than silently falling back to the working copy."""
    with pytest.raises(NoWorkflowTestVersionError, match="demo"):
        run_workflow_test("demo")


# ── the working copy ─────────────────────────────────────────────────────────

def _write_working_copy(demo, stage_dicts):
    compiled = demo / "compiled"
    compiled.mkdir(parents=True, exist_ok=True)
    for index, stage in enumerate(stage_dicts, start=1):
        (compiled / f"{index:02d}_{stage['id']}.json").write_text(
            json.dumps(stage), encoding="utf-8")


def test_working_copy_runs_with_no_version_stored_at_all(demo):
    """Runs the stages the authoring tools edit, with nothing saved as a version."""
    _write_working_copy(demo, [_load_stage(demo), _CLASSIFY])
    result = run_workflow_test("demo", use_working_copy=True)
    assert result["ok"] is True
    assert result["stages_run"] == ["classify"]
    # The working copy is frozen first, so the run pins a real snapshot rather than
    # recording no version at all.
    assert result["version_id"] is not None
    manifest = json.loads(
        (demo / "runs" / result["run_id"] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["workflow_version"] == result["version_id"]
    assert manifest["is_test_run"] is True


def test_the_minted_version_holds_the_working_copys_stages_unpublished(demo):
    """The snapshot is of what ran; publishing stays a human act."""
    _write_working_copy(demo, [_load_stage(demo), _CLASSIFY])
    result = run_workflow_test("demo", use_working_copy=True)
    version = load_version(demo, result["version_id"])
    assert [stage.id for stage in version.stages] == ["load", "classify"]
    assert version.published is False


def test_a_minted_version_is_marked_apart_from_an_authored_one(demo):
    """A structured field, not the wording of `message`, tells the two apart."""
    _seed(demo, [_load_stage(demo), _CLASSIFY])
    _write_working_copy(demo, [_load_stage(demo), _CLASSIFY])
    minted = run_workflow_test("demo", use_working_copy=True)["version_id"]
    assert load_version(demo, minted).minted_for_workflow_test is True
    assert load_version(demo, "v1").minted_for_workflow_test is False


def test_a_working_copy_runs_stage_definitions_resolve_off_its_manifest(demo):
    """What the pinned version buys: the run panel can read what each stage ran."""
    # With workflow_version null this raised RunVersionUnresolvableError, so the run
    # page had no stage definitions, no diff and no code view.
    _write_working_copy(demo, [_load_stage(demo), _CLASSIFY])
    result = run_workflow_test("demo", use_working_copy=True)
    manifest = json.loads(
        (demo / "runs" / result["run_id"] / "manifest.json").read_text(encoding="utf-8"))
    assert [stage.id for stage in load_run_stages("demo", manifest)] == ["load", "classify"]


def test_a_scoped_working_copy_run_still_freezes_the_whole_workflow(demo):
    """The version snapshots what was AUTHORED; `stages_run` is what executed."""
    _write_working_copy(demo, [_load_stage(demo), _CLASSIFY])
    result = run_workflow_test("demo", use_working_copy=True, only_stages=["load"])
    assert result["stages_run"] == ["load"]
    assert [s.id for s in load_version(demo, result["version_id"]).stages] == [
        "load", "classify"]


def test_working_copy_runs_the_edit_the_stored_version_does_not_carry(demo):
    """Genuinely different workflows: only the working copy carries `boom`."""
    _seed(demo, [_load_stage(demo), _CLASSIFY])
    _write_working_copy(demo, [_load_stage(demo), _BOOM])
    assert run_workflow_test("demo")["ok"] is True
    assert run_workflow_test("demo", use_working_copy=True)["ok"] is False


def test_naming_both_a_version_and_the_working_copy_is_refused(demo):
    """Two different workflows asked for at once — neither is chosen for the caller."""
    _seed(demo, [_load_stage(demo), _CLASSIFY])
    _write_working_copy(demo, [_load_stage(demo), _CLASSIFY])
    with pytest.raises(WorkflowTestTargetConflictError, match="v1"):
        run_workflow_test("demo", version_id="v1", use_working_copy=True)


# ── the injected slice is materialised like any other stage output ───────────

def test_the_input_stages_slice_is_written_out_as_its_own_output(demo):
    """Injected rather than computed, but still this run's input stage output."""
    _seed(demo, [_load_stage(demo), _CLASSIFY])
    result = run_workflow_test("demo", limit=2, offset=1)
    run_dir = demo / "runs" / result["run_id"]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    records = {r["stage_id"]: r for r in manifest["stage_records"]}
    assert set(records) == {"load", "classify"}
    load = records["load"]
    assert load["status"] == "ok"
    assert load["output_row_count"] == 2
    assert (run_dir / load["output_path"]).is_file()
    assert pd.read_parquet(run_dir / load["output_path"])["doc_id"].tolist() == ["b", "c"]
    # `stages_run` still reports what EXECUTED — the input was not run.
    assert result["stages_run"] == ["classify"]


# ── only_stages: scoping which stages execute ────────────────────────────────

def _load_row_count(demo, result):
    run_dir = demo / "runs" / result["run_id"]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    return next(r for r in manifest["stage_records"] if r["stage_id"] == "load")


def test_a_source_the_run_executes_is_not_read_as_an_injection(demo):
    """Only a source OUTSIDE the scope is injected; a scoped one is the run's to read."""
    stages = [parse_stage(_load_stage(demo)), parse_stage(_CLASSIFY)]
    assert list(get_source_data_with_limit_and_offset(
        stages, limit=2, offset=0, executed_ids=set())) == ["load"]
    assert get_source_data_with_limit_and_offset(
        stages, limit=2, offset=0, executed_ids={"load"}) == {}


def test_a_scope_naming_the_input_stage_reads_the_WHOLE_file_not_the_limit(demo):
    """An input INSIDE the scope executes, so its output holds the FILE, not a slice."""
    # 4 rows out of a limit of 2: read_input_data ran, the injection did not. That is
    # what makes an input column's complete vocabulary observable off the run.
    _seed(demo, [_load_stage(demo), _CLASSIFY])
    result = run_workflow_test("demo", only_stages=["load"], limit=2)
    assert result["ok"] is True
    assert result["stages_run"] == ["load"]
    record = _load_row_count(demo, result)
    assert record["status"] == "ok"
    assert record["output_row_count"] == 4
    run_dir = demo / "runs" / result["run_id"]
    assert pd.read_parquet(run_dir / record["output_path"])["doc_id"].tolist() == [
        "a", "b", "c", "d"]
    # Nothing downstream ran, so no LLM-priced stage was paid for to see the input.
    assert "classify" not in {r["stage_id"] for r in json.loads(
        (run_dir / "manifest.json").read_text(encoding="utf-8"))["stage_records"]}


def test_the_injected_slice_is_unchanged_when_the_input_is_outside_the_scope(demo):
    """limit/offset still page an input the run does NOT execute."""
    _seed(demo, [_load_stage(demo), _CLASSIFY])
    result = run_workflow_test("demo", only_stages=["classify"], limit=2, offset=1)
    assert result["stages_run"] == ["classify"]
    record = _load_row_count(demo, result)
    assert record["output_row_count"] == 2


def test_omitting_the_scope_leaves_todays_behaviour_exactly(demo):
    """Omitted, the scope is the whole frontier over an injected slice, as before."""
    # The input is not executed, and its output is the `limit` rows, not the file.
    _seed(demo, [_load_stage(demo), _CLASSIFY, _PUBLISH])
    result = run_workflow_test("demo", limit=2)
    assert result["stages_run"] == ["classify", "publish_report"]
    assert _load_row_count(demo, result)["output_row_count"] == 2


def test_a_scope_naming_an_unknown_stage_is_refused(demo):
    """A typo'd id names no stage: nothing runs, and the message lists the real ones."""
    _seed(demo, [_load_stage(demo), _CLASSIFY])
    with pytest.raises(WorkflowTestStageScopeError, match="classifyy"):
        run_workflow_test("demo", only_stages=["classifyy"])
    assert not (demo / "runs").exists()


def test_a_scope_omitting_a_computed_upstream_is_refused_naming_it(demo):
    """An upstream that cannot be injected must be scoped too, or the scope is refused."""
    # `publish_report` reads `classify`, which is not an input_data stage. Refused up
    # front, rather than a run where publish_report merely errored on a missing input.
    _seed(demo, [_load_stage(demo), _CLASSIFY, _PUBLISH])
    with pytest.raises(WorkflowTestStageScopeError, match="classify"):
        run_workflow_test("demo", only_stages=["publish_report"])
    assert not (demo / "runs").exists()


def test_a_refused_scope_on_the_working_copy_mints_no_version(demo):
    """The snapshot is taken only once the run is certain to start."""
    _write_working_copy(demo, [_load_stage(demo), _CLASSIFY])
    with pytest.raises(WorkflowTestStageScopeError):
        run_workflow_test("demo", use_working_copy=True, only_stages=["nope"])
    assert list_versions(demo) == []


def test_a_scope_may_omit_an_input_upstream_because_it_is_injectable(demo):
    """The complement of the test above: an input_data upstream may stay outside."""
    # `load` IS an input_data stage, so scoping to `classify` alone is accepted and
    # the slice is injected as usual.
    _seed(demo, [_load_stage(demo), _CLASSIFY])
    assert run_workflow_test("demo", only_stages=["classify"])["ok"] is True


def test_a_scope_holding_both_the_input_and_its_consumer_executes_both(demo):
    """The input executes whole, so the downstream stage runs over ALL of it."""
    # 4 rows, not the 2-row slice an unscoped run would have injected.
    _seed(demo, [_load_stage(demo), _CLASSIFY])
    result = run_workflow_test("demo", only_stages=["classify", "load"], limit=2)
    assert result["ok"] is True
    # Topologically sorted, so the input comes first whatever order it was named in.
    assert result["stages_run"] == ["load", "classify"]
    run_dir = demo / "runs" / result["run_id"]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    rows = {r["stage_id"]: r["output_row_count"] for r in manifest["stage_records"]}
    assert rows == {"load": 4, "classify": 4}

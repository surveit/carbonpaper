"""Tests for app/evals/store.py — eval config/run storage (document store) and
the status rule. Config/run storage is scoped by a project dir (`tmp_path`
here; only its `.name` is used, to key documents), isolated per test by the
autouse in-memory store (see conftest.fresh_store). Dataset uploads stay on
disk under `tmp_path` and are untouched by this conversion."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.models.records.eval_config import EvalConfig
from app.models.records.eval_run import EvalRun
from app.core.persistence import get_store
from app.evals.compatibility import CompatibilityReport
from app.services.workspace import resolve_project_dir
from app.evals.store import (
    EvalConfigEntry,
    eval_status,
    latest_version_id,
    list_eval_configs,
    list_eval_runs,
    load_eval_config,
    load_eval_run,
    save_dataset_upload,
    save_eval_config,
    save_eval_run,
)
from app.models.records.workflow_version import WorkflowVersion


def _ref(path="x.csv", cols=("k",)):
    return {"path": path, "format": "csv",
            "table_schema": {"columns": [{"name": c, "type": "str", "nullable": True} for c in cols]}}


_STORE_FIELDS = {"id", "created_at", "updated_at"}


def _config(**over):
    base = {
        "eval_id": "scoring", "project": "lobbymap", "name": "n",
        "override_stage": "evidence_with_benchmarks", "target_stage": "benchmark_scoring",
        "table": _ref(cols=["evidence_id", "benchmark_id", "quote", "expected_score"]),
        "expected_outputs": [{"output_column": "score", "metric": "abs_tol", "tolerance": 1}],
    }
    base.update(over)
    return EvalConfig.model_validate(base)


def _run(**over):
    base = {
        "run_id": "run-1", "config": "scoring", "project": "lobbymap",
        "workflow_version": "v1", "status": "scored",
        "settings": {"can_score_declaratively": True,
                     "frontier": ["benchmark_scoring"], "blocking_stages": []},
        "started_at": "2026-01-01T00:00:00",
    }
    base.update(over)
    return EvalRun.model_validate(base)


# ── save / list / load roundtrip ─────────────────────────────────────────────
def test_save_list_load_roundtrip(tmp_path: Path):
    config = _config()
    save_eval_config(tmp_path.name, config)

    entries = list_eval_configs(tmp_path.name)
    assert len(entries) == 1
    assert entries[0].config is not None
    assert entries[0].config.eval_id == "scoring"
    assert entries[0].issues == []
    assert entries[0].id == "scoring"

    loaded = load_eval_config(tmp_path.name, "scoring")
    assert loaded.model_dump(exclude=_STORE_FIELDS) == config.model_dump(exclude=_STORE_FIELDS)
    assert loaded.id == f"{tmp_path.name}/scoring"


def test_a_running_eval_run_round_trips_carrying_nothing_it_has_not_learned_yet(
    tmp_path: Path,
):
    run = _run(run_id="run_in_flight", status="running", finished_at=None)
    save_eval_run(tmp_path.name, run)

    loaded = load_eval_run(tmp_path.name, "run_in_flight")
    assert loaded.status == "running"
    assert loaded.metrics == {}
    assert loaded.result_ref is None
    assert loaded.finished_at is None
    assert loaded.started_at == "2026-01-01T00:00:00"


def test_save_eval_config_overwrite_allowed(tmp_path: Path):
    save_eval_config(tmp_path.name, _config())
    updated = _config(name="new name")
    save_eval_config(tmp_path.name, updated)
    loaded = load_eval_config(tmp_path.name, "scoring")
    assert loaded.name == "new name"
    # overwrite, not a second document
    assert [e.id for e in list_eval_configs(tmp_path.name)] == ["scoring"]


def test_save_eval_config_excludes_none_fields_from_the_stored_doc(tmp_path: Path):
    save_eval_config(tmp_path.name, _config())
    data = get_store().read("eval", f"{tmp_path.name}/scoring")
    assert data["eval_id"] == "scoring"
    assert data["id"] == f"{tmp_path.name}/scoring"
    assert data["override_stage"] == "evidence_with_benchmarks"
    # exclude_none=True: reference_overrides default is [] (kept, not None-valued);
    # description is None and should be excluded entirely.
    assert "description" not in data


def test_load_eval_config_missing_raises_file_not_found(tmp_path: Path):
    with pytest.raises(FileNotFoundError) as exc:
        load_eval_config(tmp_path.name, "nope")
    assert "nope" in str(exc.value)
    assert tmp_path.name in str(exc.value)


def test_load_eval_config_invalid_schema_raises_value_error(tmp_path: Path):
    get_store().write("eval", f"{tmp_path.name}/broken", {"id": "broken"})
    with pytest.raises(ValueError) as exc:
        load_eval_config(tmp_path.name, "broken")
    assert "broken" in str(exc.value)


# ── list_eval_configs tolerance ──────────────────────────────────────────────
def test_list_eval_configs_tolerates_invalid_document_others_still_load(tmp_path: Path):
    save_eval_config(tmp_path.name, _config())
    # valid JSON, but not a valid EvalConfig (missing required fields)
    get_store().write("eval", f"{tmp_path.name}/broken", {"id": "broken"})

    entries = list_eval_configs(tmp_path.name)
    assert len(entries) == 2
    by_id = {e.id: e for e in entries}

    good = by_id["scoring"]
    assert good.config is not None
    assert good.issues == []

    broken = by_id["broken"]
    assert broken.config is None
    assert broken.issues != []


def test_list_eval_configs_empty_store_returns_empty(tmp_path: Path):
    assert list_eval_configs(tmp_path.name) == []


# ── save_dataset_upload immutability ──────────────────────────────────────────
def test_save_dataset_upload_writes_file(tmp_path: Path):
    path = save_dataset_upload(tmp_path.name, "eval_dataset.csv", b"a,b\n1,2\n")
    assert path == resolve_project_dir(tmp_path.name) / "eval_data" / "eval_dataset.csv"
    assert path.read_bytes() == b"a,b\n1,2\n"


def test_save_dataset_upload_raises_file_exists_on_same_name(tmp_path: Path):
    path = save_dataset_upload(tmp_path.name, "eval_dataset.csv", b"a,b\n1,2\n")
    with pytest.raises(FileExistsError):
        save_dataset_upload(tmp_path.name, "eval_dataset.csv", b"different content")
    # original content untouched
    assert path.read_bytes() == b"a,b\n1,2\n"


@pytest.mark.parametrize("bad_name", [
    "../escape.csv", "sub/dir.csv", "sub\\dir.csv", "", "..", ".",
])
def test_save_dataset_upload_rejects_non_slugish_filenames(tmp_path: Path, bad_name):
    with pytest.raises(ValueError):
        save_dataset_upload(tmp_path.name, bad_name, b"content")


# ── save / load eval run roundtrip ────────────────────────────────────────────
def test_save_load_eval_run_roundtrip(tmp_path: Path):
    run = _run()
    save_eval_run(tmp_path.name, run)

    loaded = load_eval_run(tmp_path.name, "run-1")
    assert loaded.model_dump(exclude=_STORE_FIELDS) == run.model_dump(exclude=_STORE_FIELDS)


def test_load_eval_run_missing_raises_file_not_found(tmp_path: Path):
    with pytest.raises(FileNotFoundError) as exc:
        load_eval_run(tmp_path.name, "nope")
    assert "nope" in str(exc.value)
    assert tmp_path.name in str(exc.value)


def test_load_eval_run_invalid_schema_raises_value_error(tmp_path: Path):
    get_store().write("eval_run", f"{tmp_path.name}/broken", {"id": "broken"})
    with pytest.raises(ValueError) as exc:
        load_eval_run(tmp_path.name, "broken")
    assert "broken" in str(exc.value)


def test_load_eval_run_ignores_sibling_invalid_run(tmp_path: Path):
    wanted = _run(run_id="run-good")
    save_eval_run(tmp_path.name, wanted)
    get_store().write("eval_run", f"{tmp_path.name}/run-bad", {"id": "run-bad"})

    loaded = load_eval_run(tmp_path.name, "run-good")
    assert loaded.model_dump(exclude=_STORE_FIELDS) == wanted.model_dump(exclude=_STORE_FIELDS)


@pytest.mark.parametrize("bad_id", [
    "../escape", "sub/dir", "sub\\dir", "", "..", ".",
])
def test_load_eval_run_rejects_non_slugish_run_id(tmp_path: Path, bad_id):
    with pytest.raises(ValueError):
        load_eval_run(tmp_path.name, bad_id)


# ── list_eval_runs ────────────────────────────────────────────────────────────
def test_list_eval_runs_filters_by_config_and_sorts_newest_first(tmp_path: Path):
    r_old = _run(run_id="run-old", started_at="2026-01-01T00:00:00")
    r_new = _run(run_id="run-new", started_at="2026-02-01T00:00:00")
    r_other_config = _run(run_id="run-other", config="other-config")
    for r in (r_old, r_new, r_other_config):
        save_eval_run(tmp_path.name, r)

    runs = list_eval_runs(tmp_path.name, "scoring")
    assert [r.run_id for r in runs] == ["run-new", "run-old"]


def test_list_eval_runs_none_stored_returns_empty(tmp_path: Path):
    assert list_eval_runs(tmp_path.name, "scoring") == []


def test_list_eval_runs_sorts_by_started_at_then_id_when_missing(tmp_path: Path):
    r_no_start_a = _run(run_id="run-a", started_at=None)
    r_no_start_b = _run(run_id="run-b", started_at=None)
    for r in (r_no_start_a, r_no_start_b):
        save_eval_run(tmp_path.name, r)

    runs = list_eval_runs(tmp_path.name, "scoring")
    # both have started_at=None -> "" -> tiebreak by id, newest-first means
    # sorted descending on (started_at or "", id)
    assert [r.run_id for r in runs] == ["run-b", "run-a"]


# ── latest_version_id ─────────────────────────────────────────────────────────
def test_latest_version_id_none_when_no_versions(tmp_path: Path):
    assert latest_version_id(tmp_path.name) is None


def test_latest_version_id_returns_newest(tmp_path: Path):
    for vid in ("20260101T000000", "20260201T000000"):
        WorkflowVersion(id=f"{tmp_path.name}/{vid}", version_id=vid, created_at="x",
                message="m").save()
    assert latest_version_id(tmp_path.name) == "20260201T000000"


def test_latest_version_id_returns_the_only_version(tmp_path: Path):
    WorkflowVersion(id=f"{tmp_path.name}/20260101T000000", version_id="20260101T000000",
            created_at="x", message="m").save()
    assert latest_version_id(tmp_path.name) == "20260101T000000"


# ── eval_status matrix ────────────────────────────────────────────────────────
def _report(ok=True, settings=None):
    return CompatibilityReport(ok=ok, problems=[] if ok else ["broken thing"],
                               settings=settings)


def test_eval_status_broken_even_with_runs():
    runs = [_run(status="scored", workflow_version="v1")]
    assert eval_status(_report(ok=False), runs, latest_version="v1",
                       has_eval_dataset=True) == "broken"


def test_eval_status_no_eval_dataset_yet():
    assert eval_status(_report(ok=True), [], latest_version=None,
                       has_eval_dataset=False) == "no eval dataset yet"


def test_eval_status_broken_beats_no_eval_dataset():
    assert eval_status(_report(ok=False), [], latest_version=None,
                       has_eval_dataset=False) == "broken"


def test_eval_status_never_run():
    assert eval_status(_report(ok=True), [], latest_version="v1",
                       has_eval_dataset=True) == "never run"


def test_eval_status_stale_when_no_latest_version():
    runs = [_run(status="scored", workflow_version="v1")]
    assert eval_status(_report(ok=True), runs, latest_version=None,
                       has_eval_dataset=True) == "stale"


def test_eval_status_stale_when_version_mismatch():
    runs = [_run(status="scored", workflow_version="v1")]
    assert eval_status(_report(ok=True), runs, latest_version="v2",
                       has_eval_dataset=True) == "stale"


@pytest.mark.parametrize("status", ["error", "vetoed"])
def test_eval_status_run_errored(status):
    runs = [_run(status=status, workflow_version="v1")]
    assert eval_status(_report(ok=True), runs, latest_version="v1",
                       has_eval_dataset=True) == "run errored"


def test_eval_status_reports_running_while_the_latest_run_is_in_flight():
    runs = [_run(status="running", workflow_version="v1", finished_at=None)]
    assert eval_status(_report(ok=True), runs, latest_version="v1",
                       has_eval_dataset=True) == "running"


def test_eval_status_running_beats_stale_because_there_is_no_verdict_yet():
    runs = [_run(status="running", workflow_version="v1", finished_at=None)]
    assert eval_status(_report(ok=True), runs, latest_version="v2",
                       has_eval_dataset=True) == "running"


def test_eval_status_run_succeeded():
    runs = [_run(status="scored", workflow_version="v1")]
    assert eval_status(_report(ok=True), runs, latest_version="v1",
                       has_eval_dataset=True) == "run succeeded"


def test_eval_config_entry_is_dataclass_shape():
    entry = EvalConfigEntry(config=None, id="x", issues=["bad"])
    assert entry.config is None
    assert entry.issues == ["bad"]

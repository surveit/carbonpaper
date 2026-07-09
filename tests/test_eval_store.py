"""Tests for app/services/eval_store.py — eval config/run storage and status
derivation. All storage lives under a tmp_path methodology dir; nothing here
touches examples/."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.models import EvalConfig, EvalRun
from app.services.eval_compat import CompatibilityReport
from app.services.eval_store import (
    EvalConfigEntry,
    eval_status,
    latest_version_id,
    list_eval_configs,
    list_eval_runs,
    load_eval_config,
    load_eval_run,
    save_dataset_upload,
    save_eval_config,
)


def _ref(path="x.csv", cols=("k",)):
    return {"path": path, "format": "csv",
            "table_schema": {"columns": [{"name": c} for c in cols]}}


def _config(**over):
    base = {
        "id": "scoring", "methodology": "lobbymap", "name": "n",
        "override_stage": "evidence_with_benchmarks", "target_stage": "benchmark_scoring",
        "table": _ref(cols=["evidence_id", "benchmark_id", "quote", "expected_score"]),
        "expected": [{"actual": "score", "metric": "abs_tol", "tolerance": 1}],
    }
    base.update(over)
    return EvalConfig.model_validate(base)


def _run(**over):
    base = {
        "id": "run-1", "config": "scoring", "methodology": "lobbymap",
        "methodology_version": "v1", "status": "scored",
        "settings": {"can_score_declaratively": True,
                     "frontier": ["benchmark_scoring"], "blocking_stages": []},
        "started_at": "2026-01-01T00:00:00",
    }
    base.update(over)
    return EvalRun.model_validate(base)


# ── save / list / load roundtrip ─────────────────────────────────────────────
def test_save_list_load_roundtrip(tmp_path: Path):
    config = _config()
    path = save_eval_config(tmp_path, config)
    assert path == tmp_path / "eval_config" / "scoring.yaml"
    assert path.is_file()

    entries = list_eval_configs(tmp_path)
    assert len(entries) == 1
    assert entries[0].config is not None
    assert entries[0].config.id == "scoring"
    assert entries[0].issues == []
    assert entries[0].path == path

    loaded = load_eval_config(tmp_path, "scoring")
    assert loaded == config


def test_save_eval_config_overwrite_allowed(tmp_path: Path):
    save_eval_config(tmp_path, _config())
    updated = _config(name="new name")
    path = save_eval_config(tmp_path, updated)
    loaded = load_eval_config(tmp_path, "scoring")
    assert loaded.name == "new name"
    assert path == tmp_path / "eval_config" / "scoring.yaml"


def test_save_eval_config_writes_yaml_safe_dump_shape(tmp_path: Path):
    path = save_eval_config(tmp_path, _config())
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["id"] == "scoring"
    assert data["override_stage"] == "evidence_with_benchmarks"
    # exclude_none=True: reference_overrides default is [] (kept, not None-valued);
    # description is None and should be excluded entirely.
    assert "description" not in data


def test_load_eval_config_missing_raises_file_not_found_with_path(tmp_path: Path):
    with pytest.raises(FileNotFoundError) as exc:
        load_eval_config(tmp_path, "nope")
    assert str(tmp_path / "eval_config" / "nope.yaml") in str(exc.value)


def test_load_eval_config_malformed_raises_value_error_with_path(tmp_path: Path):
    bad_dir = tmp_path / "eval_config"
    bad_dir.mkdir(parents=True)
    bad_path = bad_dir / "broken.yaml"
    bad_path.write_text("not: [valid, eval, config", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_eval_config(tmp_path, "broken")
    assert str(bad_path) in str(exc.value)


# ── list_eval_configs tolerance ──────────────────────────────────────────────
def test_list_eval_configs_tolerates_malformed_yaml_others_still_load(tmp_path: Path):
    save_eval_config(tmp_path, _config())
    bad_dir = tmp_path / "eval_config"
    (bad_dir / "broken.yaml").write_text("not: [valid, yaml", encoding="utf-8")

    entries = list_eval_configs(tmp_path)
    assert len(entries) == 2
    by_path_name = {e.path.name: e for e in entries}

    good = by_path_name["scoring.yaml"]
    assert good.config is not None
    assert good.issues == []

    broken = by_path_name["broken.yaml"]
    assert broken.config is None
    assert broken.issues != []


def test_list_eval_configs_tolerates_valid_yaml_invalid_schema(tmp_path: Path):
    bad_dir = tmp_path / "eval_config"
    bad_dir.mkdir(parents=True)
    # valid YAML, but not a valid EvalConfig (missing required fields)
    (bad_dir / "incomplete.yaml").write_text(
        yaml.safe_dump({"id": "incomplete"}), encoding="utf-8")

    entries = list_eval_configs(tmp_path)
    assert len(entries) == 1
    assert entries[0].config is None
    assert entries[0].issues != []


def test_list_eval_configs_empty_dir_returns_empty(tmp_path: Path):
    assert list_eval_configs(tmp_path) == []


# ── save_dataset_upload immutability ──────────────────────────────────────────
def test_save_dataset_upload_writes_file(tmp_path: Path):
    path = save_dataset_upload(tmp_path, "cases.csv", b"a,b\n1,2\n")
    assert path == tmp_path / "eval_data" / "cases.csv"
    assert path.read_bytes() == b"a,b\n1,2\n"


def test_save_dataset_upload_raises_file_exists_on_same_name(tmp_path: Path):
    save_dataset_upload(tmp_path, "cases.csv", b"a,b\n1,2\n")
    with pytest.raises(FileExistsError):
        save_dataset_upload(tmp_path, "cases.csv", b"different content")
    # original content untouched
    assert (tmp_path / "eval_data" / "cases.csv").read_bytes() == b"a,b\n1,2\n"


@pytest.mark.parametrize("bad_name", [
    "../escape.csv", "sub/dir.csv", "sub\\dir.csv", "", "..", ".",
])
def test_save_dataset_upload_rejects_non_slugish_filenames(tmp_path: Path, bad_name):
    with pytest.raises(ValueError):
        save_dataset_upload(tmp_path, bad_name, b"content")


# ── list_eval_runs ────────────────────────────────────────────────────────────
def test_list_eval_runs_filters_by_config_and_sorts_newest_first(tmp_path: Path):
    run_dir = tmp_path / "eval_run"
    run_dir.mkdir(parents=True)
    r_old = _run(id="run-old", started_at="2026-01-01T00:00:00")
    r_new = _run(id="run-new", started_at="2026-02-01T00:00:00")
    r_other_config = _run(id="run-other", config="other-config")
    for r in (r_old, r_new, r_other_config):
        (run_dir / f"{r.id}.json").write_text(r.model_dump_json(), encoding="utf-8")

    runs = list_eval_runs(tmp_path, "scoring")
    assert [r.id for r in runs] == ["run-new", "run-old"]


def test_list_eval_runs_no_run_dir_returns_empty(tmp_path: Path):
    assert list_eval_runs(tmp_path, "scoring") == []


def test_list_eval_runs_sorts_by_started_at_then_id_when_missing(tmp_path: Path):
    run_dir = tmp_path / "eval_run"
    run_dir.mkdir(parents=True)
    r_no_start_a = _run(id="run-a", started_at=None)
    r_no_start_b = _run(id="run-b", started_at=None)
    for r in (r_no_start_a, r_no_start_b):
        (run_dir / f"{r.id}.json").write_text(r.model_dump_json(), encoding="utf-8")

    runs = list_eval_runs(tmp_path, "scoring")
    # both have started_at=None -> "" -> tiebreak by id, newest-first means
    # sorted descending on (started_at or "", id)
    assert [r.id for r in runs] == ["run-b", "run-a"]


# ── load_eval_run ─────────────────────────────────────────────────────────────
def test_load_eval_run_reads_requested_run_only(tmp_path: Path):
    run_dir = tmp_path / "eval_run"
    run_dir.mkdir(parents=True)
    wanted = _run(id="run-1")
    (run_dir / "run-1.json").write_text(wanted.model_dump_json(), encoding="utf-8")

    loaded = load_eval_run(tmp_path, "run-1")
    assert loaded == wanted


def test_load_eval_run_missing_raises_file_not_found_with_path(tmp_path: Path):
    with pytest.raises(FileNotFoundError) as exc:
        load_eval_run(tmp_path, "nope")
    assert str(tmp_path / "eval_run" / "nope.json") in str(exc.value)


def test_load_eval_run_malformed_raises_value_error_with_path(tmp_path: Path):
    run_dir = tmp_path / "eval_run"
    run_dir.mkdir(parents=True)
    bad_path = run_dir / "broken.json"
    bad_path.write_text("not json at all {", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_eval_run(tmp_path, "broken")
    assert str(bad_path) in str(exc.value)


def test_load_eval_run_ignores_sibling_corrupt_run(tmp_path: Path):
    """The requested run loads fine even when another run file for the same
    methodology dir is corrupt -- load_eval_run reads only the one file
    named by run_id, unlike list_eval_runs which globs every eval_run/*.json."""
    run_dir = tmp_path / "eval_run"
    run_dir.mkdir(parents=True)
    wanted = _run(id="run-good")
    (run_dir / "run-good.json").write_text(wanted.model_dump_json(), encoding="utf-8")
    (run_dir / "run-bad.json").write_text("not json at all {", encoding="utf-8")

    loaded = load_eval_run(tmp_path, "run-good")
    assert loaded == wanted


@pytest.mark.parametrize("bad_id", [
    "../escape", "sub/dir", "sub\\dir", "", "..", ".",
])
def test_load_eval_run_rejects_non_slugish_run_id(tmp_path: Path, bad_id):
    with pytest.raises(ValueError):
        load_eval_run(tmp_path, bad_id)


# ── latest_version_id ─────────────────────────────────────────────────────────
def test_latest_version_id_none_when_no_versions(tmp_path: Path):
    assert latest_version_id(tmp_path) is None


def test_latest_version_id_returns_newest(tmp_path: Path):
    versions_dir = tmp_path / "versions"
    for vid in ("20260101T000000", "20260201T000000"):
        vdir = versions_dir / vid
        vdir.mkdir(parents=True)
        (vdir / "version.json").write_text(
            f'{{"id": "{vid}", "created_at": "x", "parent_version": null, '
            f'"message": "m", "reviewer": "r", "coverage": {{}}}}',
            encoding="utf-8")
    assert latest_version_id(tmp_path) == "20260201T000000"


# ── eval_status matrix ────────────────────────────────────────────────────────
def _report(ok=True, settings=None):
    return CompatibilityReport(ok=ok, problems=[] if ok else ["broken thing"],
                               settings=settings)


def test_eval_status_broken_even_with_runs():
    runs = [_run(status="scored", methodology_version="v1")]
    assert eval_status(_report(ok=False), runs, latest_version="v1",
                       has_cases=True) == "broken"


def test_eval_status_no_cases_yet():
    assert eval_status(_report(ok=True), [], latest_version=None,
                       has_cases=False) == "no cases yet"


def test_eval_status_broken_beats_no_cases():
    assert eval_status(_report(ok=False), [], latest_version=None,
                       has_cases=False) == "broken"


def test_eval_status_never_run():
    assert eval_status(_report(ok=True), [], latest_version="v1",
                       has_cases=True) == "never run"


def test_eval_status_stale_when_no_latest_version():
    runs = [_run(status="scored", methodology_version="v1")]
    assert eval_status(_report(ok=True), runs, latest_version=None,
                       has_cases=True) == "stale"


def test_eval_status_stale_when_version_mismatch():
    runs = [_run(status="scored", methodology_version="v1")]
    assert eval_status(_report(ok=True), runs, latest_version="v2",
                       has_cases=True) == "stale"


@pytest.mark.parametrize("status", ["error", "vetoed"])
def test_eval_status_run_errored(status):
    runs = [_run(status=status, methodology_version="v1")]
    assert eval_status(_report(ok=True), runs, latest_version="v1",
                       has_cases=True) == "run errored"


def test_eval_status_run_succeeded():
    runs = [_run(status="scored", methodology_version="v1")]
    assert eval_status(_report(ok=True), runs, latest_version="v1",
                       has_cases=True) == "run succeeded"


def test_eval_config_entry_is_dataclass_shape():
    entry = EvalConfigEntry(config=None, path=Path("x"), issues=["bad"])
    assert entry.config is None
    assert entry.issues == ["bad"]

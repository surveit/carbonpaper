"""The runs index rows (app/web/run_index.py) read off manifests on disk."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.run_status import RunStatus
from app.services import workspace
from app.web.run_index import build_run_index_rows
from run_seed import store_manifest, store_manifest_text


GOLDENS = Path(__file__).parent / "goldens"


@pytest.fixture()
def runs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workspace.set_projects_dir(tmp_path)
    runs = tmp_path / "demo" / "runs"
    runs.mkdir(parents=True)
    return runs


def _write_run(runs: Path, run_id: str, manifest: dict) -> None:
    run_dir = runs / run_id
    run_dir.mkdir()
    store_manifest(run_dir.parent.parent, run_dir.name, manifest)


def _current_manifest() -> dict[str, object]:
    return json.loads((GOLDENS / "ok_run.json").read_text(encoding="utf-8"))


def _pre_rename_manifest() -> dict[str, object]:
    manifest = _current_manifest()
    manifest["stages"] = manifest.pop("stage_records")
    return manifest


def test_valid_and_legacy_manifests_listed_side_by_side(runs_root: Path):
    _write_run(runs_root, "20260101T000000", _current_manifest())
    _write_run(runs_root, "20260101T000001", _pre_rename_manifest())

    by_id = {row.run_id: row for row in build_run_index_rows("demo")}
    assert set(by_id) == {"20260101T000000", "20260101T000001"}

    good = by_id["20260101T000000"]
    assert good.status == RunStatus.OK
    assert good.strip is not None
    assert [(s.stage_id, s.status) for s in good.strip.squares] == [("load", "ok")]
    assert good.result_summary == "1 done"
    assert good.outcome == "Complete"

    # A manifest this reader cannot parse yields an identity-only row rather than
    # counts it never read — no strip, no version, no timestamp.
    legacy = by_id["20260101T000001"]
    assert legacy.status == "corrupt"
    assert legacy.strip is None
    assert legacy.version is None
    assert legacy.started_at is None
    # No outcome word either: the result cell states the unreadability instead of
    # putting a status under a strip it never built.
    assert legacy.outcome == ""


def test_unparseable_json_is_corrupt_not_zero(runs_root: Path):
    store_manifest_text(runs_root.parent, "20260101T000002", "{ not json")

    row, = build_run_index_rows("demo")
    assert row.status == "corrupt"
    assert row.strip is None


def test_a_workflow_test_run_is_listed_and_flagged_as_one(runs_root: Path):
    test_run = _current_manifest()
    test_run["is_test_run"] = True
    _write_run(runs_root, "20260101T000003", test_run)

    row, = build_run_index_rows("demo")
    assert row.run_id == "20260101T000003"
    assert row.is_test_run is True


def test_a_manifest_predating_the_field_reads_as_not_a_test(runs_root: Path):
    _write_run(runs_root, "20260101T000004", _current_manifest())

    row, = build_run_index_rows("demo")
    assert row.is_test_run is False


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (RunStatus.OK, "Complete"),
        (RunStatus.WARNINGS, "Complete, with warnings"),
        (RunStatus.ERRORS, "Error"),
        (RunStatus.AWAITING_REVIEW, "Awaiting review"),
        (RunStatus.CANCELLED, "Cancelled"),
        (RunStatus.RUNNING, "In progress"),
    ],
)
def test_every_run_status_has_a_word_under_the_strip(
    runs_root: Path, status: RunStatus, outcome: str
):
    _write_run(runs_root, "20260101T000006", {**_current_manifest(), "status": status})

    row, = build_run_index_rows("demo")
    assert row.outcome == outcome


def test_an_unresolvable_pinned_version_says_so_instead_of_a_message(runs_root: Path):
    # The golden pins a version that does not exist in this workspace.
    _write_run(runs_root, "20260101T000007", _current_manifest())

    row, = build_run_index_rows("demo")
    assert row.version is not None
    assert row.version.message is None
    assert row.version.error is not None
    assert "could not be read" in row.version.error


def test_no_runs_dir_lists_nothing(tmp_path: Path):
    workspace.set_projects_dir(tmp_path)
    assert build_run_index_rows("nothing_here") == []

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.run_status import RunStatus
from app.web import loading


GOLDENS = Path(__file__).parent / "goldens"


@pytest.fixture()
def runs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """EXAMPLES_DIR repointed at a tmp workspace holding one project, `demo`."""
    monkeypatch.setattr(loading, "EXAMPLES_DIR", tmp_path)
    runs = tmp_path / "demo" / "runs"
    runs.mkdir(parents=True)
    return runs


def _write_run(runs: Path, run_id: str, manifest: object) -> None:
    run_dir = runs / run_id
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


def _current_manifest() -> dict[str, object]:
    return json.loads((GOLDENS / "ok_run.json").read_text(encoding="utf-8"))


def _pre_rename_manifest() -> dict[str, object]:
    """The same run as it was persisted BEFORE this branch renamed the on-disk
    keys: `stage_records` was `stages`."""
    manifest = _current_manifest()
    manifest["stages"] = manifest.pop("stage_records")
    return manifest


def test_valid_and_legacy_manifests_listed_side_by_side(runs_root: Path):
    """The current-format run reports its true counts; the pre-rename run is
    marked corrupt with no counts at all — and reading the index does not
    raise."""
    _write_run(runs_root, "20260101T000000", _current_manifest())
    _write_run(runs_root, "20260101T000001", _pre_rename_manifest())

    by_id = {e["run_id"]: e for e in loading.list_runs("demo")}
    assert set(by_id) == {"20260101T000000", "20260101T000001"}

    good = by_id["20260101T000000"]
    assert good["status"] == RunStatus.OK
    assert (good["stages_total"], good["stages_ok"], good["stages_error"]) == (1, 1, 0)

    legacy = by_id["20260101T000001"]
    assert legacy["status"] == "corrupt"
    assert legacy["stages_total"] is None
    assert legacy["stages_ok"] is None
    assert legacy["stages_error"] is None


def test_unparseable_json_is_corrupt_not_zero(runs_root: Path):
    run_dir = runs_root / "20260101T000002"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text("{ not json", encoding="utf-8")

    entry, = loading.list_runs("demo")
    assert entry["status"] == "corrupt"
    assert entry["stages_total"] is None


def test_a_workflow_test_run_is_listed_and_marked_is_test_run(runs_root: Path):
    """A workflow test writes into runs/ exactly like a production run — it
    appears in the same listing, reachable through the same row shape — but its
    row carries `is_test_run: True` so the template can mark it."""
    test_run = _current_manifest()
    test_run["is_test_run"] = True
    _write_run(runs_root, "20260101T000003", test_run)

    entry, = loading.list_runs("demo")
    assert entry["run_id"] == "20260101T000003"
    assert entry["is_test_run"] is True


def test_a_manifest_predating_the_field_reads_as_not_a_test(runs_root: Path):
    """No `is_test_run` key at all (every manifest on disk before this field
    existed) is not a test — the default, not a fabricated guess."""
    _write_run(runs_root, "20260101T000004", _current_manifest())

    entry, = loading.list_runs("demo")
    assert entry["is_test_run"] is False

"""One parser, three policies. app.models.run_manifest owns the single read of a run
directory's manifest.json; what an unreadable or untypable one MEANS is each caller's
own answer, and the three answers differ on purpose. Pinned here side by side, because
folding them onto one policy silently changes what a page shows.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import project as project_service
from app.services import workspace
from app.web import loading
from app.web.run_index import build_run_index_rows

GOLDENS = Path(__file__).parent / "goldens"


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    workspace.set_projects_dir(tmp_path)
    pdir = tmp_path / "demo"
    (pdir / "runs").mkdir(parents=True)
    (pdir / "document.md").write_text("methodology", encoding="utf-8")
    return pdir


def _write_run(pdir: Path, run_id: str, manifest_text: str) -> None:
    run_dir = pdir / "runs" / run_id
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text(manifest_text, encoding="utf-8")


def _ok_run_manifest() -> dict[str, object]:
    return json.loads((GOLDENS / "ok_run.json").read_text(encoding="utf-8"))


# ─── A manifest.json whose bytes are not JSON ────────────────────────────────


def test_the_runs_summary_counts_an_unparseable_manifest_as_a_corrupt_run(project_dir: Path):
    _write_run(project_dir, "20260101T000000", "{ not json")

    summary = project_service._runs_summary(project_dir)
    assert (summary.n, summary.latest_status) == (1, "corrupt")


def test_the_dashboard_card_drops_an_unparseable_manifest_from_its_run_count(project_dir: Path):
    _write_run(project_dir, "20260101T000000", "{ not json")

    card, = loading.list_projects()
    assert card.n_runs == 0


def test_the_runs_index_lists_an_unparseable_manifest_as_an_identity_only_row(project_dir: Path):
    _write_run(project_dir, "20260101T000000", "{ not json")

    row, = build_run_index_rows("demo")
    assert (row.run_id, row.status, row.strip) == ("20260101T000000", "corrupt", None)


# ─── A manifest.json that is JSON, but not a shape RunManifest accepts ───────
# The parse LEVEL differs, not just the corrupt policy: the two counting readers
# take one raw fact off a manifest the typed model rejects, so a run persisted
# before `stage_records` was renamed still reports its real status. Only the index,
# which needs the whole model to draw a stage strip, calls such a run unreadable.


def test_the_two_counting_readers_still_read_a_pre_rename_manifests_real_status(project_dir: Path):
    manifest = _ok_run_manifest()
    manifest["stages"] = manifest.pop("stage_records")
    _write_run(project_dir, "20260101T000000", json.dumps(manifest))

    summary = project_service._runs_summary(project_dir)
    assert (summary.n, summary.latest_status) == (1, "ok")
    card, = loading.list_projects()
    assert card.n_runs == 1


def test_the_runs_index_calls_a_pre_rename_manifest_corrupt(project_dir: Path):
    manifest = _ok_run_manifest()
    manifest["stages"] = manifest.pop("stage_records")
    _write_run(project_dir, "20260101T000000", json.dumps(manifest))

    row, = build_run_index_rows("demo")
    assert (row.status, row.strip) == ("corrupt", None)


# ─── A workflow test's run ───────────────────────────────────────────────────


def test_the_runs_index_lists_a_test_run_the_two_counting_readers_exclude(project_dir: Path):
    manifest = _ok_run_manifest()
    manifest["parameters"] = {"is_test_run": True}
    _write_run(project_dir, "20260101T000000", json.dumps(manifest))

    row, = build_run_index_rows("demo")
    assert (row.status, row.is_test_run) == ("ok", True)
    assert project_service._runs_summary(project_dir).n == 0
    card, = loading.list_projects()
    assert card.n_runs == 0

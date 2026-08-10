"""A workflow test's run (RunManifest.is_test_run=True, under the project's own
runs/ dir) must never inflate or masquerade as the project's real run counts:
RunsSummary (app.services.project) and the dashboard card's n_runs
(app.web.loading.list_projects) both count non-test runs only."""
from __future__ import annotations

from pathlib import Path

from app.services import project as project_service
from app.services import workspace
from app.web import loading
from app.services.methodology import write_methodology
from run_seed import store_manifest


def _write_manifest(
    run_dir: Path, *, status: str, is_test_run: bool | None, legacy: bool = False
) -> None:
    """`legacy` writes the flat pre-nesting key, which runs on disk today still carry."""
    manifest: dict[str, object] = {"status": status}
    if is_test_run is not None:
        manifest |= ({"is_test_run": is_test_run} if legacy
                     else {"parameters": {"is_test_run": is_test_run}})
    store_manifest(run_dir.parent.parent, run_dir.name, manifest)


def test_runs_summary_excludes_test_runs_from_every_count(tmp_path):
    """A production run + a later (newer) workflow test: n/latest_status/
    awaiting_review all read as if the test run were never there."""
    runs = tmp_path / "runs"
    _write_manifest(runs / "20260101T000000", status="ok", is_test_run=None)
    _write_manifest(runs / "20260102T000000", status="awaiting_review", is_test_run=True)

    summary = project_service._runs_summary(tmp_path.name)
    assert summary.n == 1
    assert summary.awaiting_review == 0
    assert summary.latest_status == "ok"  # the newer row is excluded, not counted


def test_runs_summary_with_only_test_runs_reports_no_runs(tmp_path):
    runs = tmp_path / "runs"
    _write_manifest(runs / "20260101T000000", status="ok", is_test_run=True)

    summary = project_service._runs_summary(tmp_path.name)
    assert summary.n == 0
    assert summary.latest_status is None


def test_dashboard_card_n_runs_excludes_test_runs(tmp_path, monkeypatch):
    workspace.set_projects_dir(tmp_path)
    root = tmp_path / "demo"
    root.mkdir()
    write_methodology(root.name, "methodology")
    _write_manifest(root / "runs" / "20260101T000000", status="ok", is_test_run=None)
    _write_manifest(root / "runs" / "20260102T000000", status="ok", is_test_run=True)

    card, = loading.list_projects()
    assert card["n_runs"] == 1


def test_a_legacy_manifests_flat_flag_still_excludes_it(tmp_path):
    """Else every historical workflow test starts counting as a real run."""
    workspace.set_projects_dir(tmp_path)
    root = tmp_path / "demo"
    root.mkdir()
    write_methodology(root.name, "methodology")
    _write_manifest(root / "runs" / "20260101T000000", status="ok",
                    is_test_run=None, legacy=True)
    _write_manifest(root / "runs" / "20260102T000000", status="ok",
                    is_test_run=True, legacy=True)

    assert project_service._runs_summary(root.name).n == 1
    card, = loading.list_projects()
    assert card["n_runs"] == 1

"""A workflow test's run (RunManifest.of_record=False, under the project's own
runs/ dir) must never inflate or masquerade as the project's run-of-record
counts: RunsSummary (app.services.project) and the dashboard card's n_runs
(app.web.loading.list_projects) both count runs of record only."""
from __future__ import annotations

import json
from pathlib import Path

from app.services import project as project_service
from app.web import loading


def _write_manifest(run_dir: Path, *, status: str, of_record: bool | None) -> None:
    run_dir.mkdir(parents=True)
    manifest: dict[str, object] = {"status": status}
    if of_record is not None:
        manifest["of_record"] = of_record
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_runs_summary_excludes_test_runs_from_every_count(tmp_path):
    """A production run + a later (newer) workflow test: n/latest_status/
    awaiting_review all read as if the test run were never there."""
    runs = tmp_path / "runs"
    _write_manifest(runs / "20260101T000000", status="ok", of_record=None)
    _write_manifest(runs / "20260102T000000", status="awaiting_review", of_record=False)

    summary = project_service._runs_summary(tmp_path)
    assert summary.n == 1
    assert summary.awaiting_review == 0
    assert summary.latest_status == "ok"  # the newer row is excluded, not counted


def test_runs_summary_with_only_test_runs_reports_no_runs(tmp_path):
    runs = tmp_path / "runs"
    _write_manifest(runs / "20260101T000000", status="ok", of_record=False)

    summary = project_service._runs_summary(tmp_path)
    assert summary.n == 0
    assert summary.latest_status is None


def test_dashboard_card_n_runs_excludes_test_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(loading, "EXAMPLES_DIR", tmp_path)
    root = tmp_path / "demo"
    root.mkdir()
    (root / "document.md").write_text("methodology", encoding="utf-8")
    _write_manifest(root / "runs" / "20260101T000000", status="ok", of_record=None)
    _write_manifest(root / "runs" / "20260102T000000", status="ok", of_record=False)

    card, = loading.list_projects()
    assert card["n_runs"] == 1

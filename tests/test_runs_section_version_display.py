"""The RUNS section table shows each run's actual workflow version — not the
"(unversioned)" fallback — because `section_runs.html` must read the key
`app.web.loading.list_runs` actually emits (`workflow_version`), not a
retired `dag_version` name."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import workspace
from run_seed import store_manifest

client = TestClient(app)

GOLDENS = Path(__file__).parent / "goldens"


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `demo` project with one real, manifest-backed run."""
    pdir = tmp_path / "demo"
    pdir.mkdir(parents=True)
    workspace.set_projects_dir(tmp_path)

    manifest = json.loads((GOLDENS / "ok_run.json").read_text(encoding="utf-8"))
    run_dir = pdir / "runs" / manifest["run_id"]
    run_dir.mkdir(parents=True)
    store_manifest(run_dir.parent.parent, run_dir.name, manifest)
    return pdir


def test_runs_section_shows_the_actual_workflow_version(project: Path) -> None:
    manifest = json.loads((GOLDENS / "ok_run.json").read_text(encoding="utf-8"))
    page = client.get("/project/demo/runs")
    assert page.status_code == 200
    assert manifest["workflow_version"] in page.text
    assert "(unversioned)" not in page.text

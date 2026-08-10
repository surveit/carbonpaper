from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app.seeds.seed import discover_workflow_files, seed_all, seed_demo_data_if_enabled
from app.services import project

_LOBBYING = "lobbying_issue_triage"
_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_seed_cli_subprocess_bootstraps_the_store_and_seeds(tmp_path):
    """In-process the autouse store fixture masks the bug; only a subprocess is store-free."""
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir()
    env = {
        **os.environ,
        "CARBONPAPER_PROJECTS_DIR": str(examples_dir),
        "CARBONPAPER_DB_PATH": str(tmp_path / "app.db"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "app.seeds"],
        cwd=_REPO_ROOT, env=env, capture_output=True, text=True,
    )

    assert result.returncode == 0, (
        f"seed CLI crashed in a store-free process:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert f"imported: {_LOBBYING}" in result.stdout
    assert (examples_dir / _LOBBYING / "compiled").is_dir()


def test_seed_all_imports_the_lobbying_bundle_into_an_empty_workspace(tmp_path):
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir()

    imported = seed_all()

    assert imported == [_LOBBYING]
    assert _LOBBYING in project.list_projects()


def test_seed_all_skips_a_bundle_whose_project_already_exists(tmp_path):
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir()
    first = seed_all()
    assert first == [_LOBBYING]

    second = seed_all()

    assert second == []
    assert _LOBBYING in project.list_projects()


def test_discover_workflow_files_finds_the_committed_lobbying_fixture():
    found = discover_workflow_files()

    names = {wf_path.stem for wf_path in found}
    assert _LOBBYING in names
    lobbying_path = next(wf_path for wf_path in found if wf_path.stem == _LOBBYING)
    assert lobbying_path.suffix == ".json"
    assert lobbying_path.is_file()


def test_discover_workflow_files_filters_to_json_files(tmp_path):
    (tmp_path / "alpha.json").write_text("{}", encoding="utf-8")
    (tmp_path / "beta_a_directory").mkdir()  # a dir, not a fixture
    (tmp_path / "gamma.csv").write_text("a,b\n1,2\n", encoding="utf-8")  # sibling data, not a fixture

    found = discover_workflow_files(data_dir=tmp_path)

    assert found == [tmp_path / "alpha.json"]


def test_discover_workflow_files_returns_empty_list_for_a_missing_data_dir(tmp_path):
    assert discover_workflow_files(data_dir=tmp_path / "does_not_exist") == []


def test_seed_demo_data_if_enabled_is_a_noop_when_env_var_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("CARBONPAPER_SEED_DEMO", raising=False)
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir()

    imported = seed_demo_data_if_enabled()

    assert imported == []
    assert project.list_projects() == []


def test_seed_demo_data_if_enabled_seeds_when_env_var_is_1(tmp_path, monkeypatch):
    monkeypatch.setenv("CARBONPAPER_SEED_DEMO", "1")
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir()

    imported = seed_demo_data_if_enabled()

    assert imported == [_LOBBYING]
    assert _LOBBYING in project.list_projects()

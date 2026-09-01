"""The stage panel must show what a run READ, not what its version authored: a
run binding replaces connector params, and every reader of the run's stages
replays it — the same delta `resume_run` replays."""
from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

import app.services.run as run_service
from app.core.errors import RunVersionUnresolvableError
from app.services import workspace
from app.services.project import save_working_copy_as_version
from stage_seed import add_stage


@pytest.fixture
def project(tmp_path):
    proj = tmp_path / "demo"
    proj.mkdir(parents=True, exist_ok=True)
    authored = proj / "authored.csv"
    pd.DataFrame({"name": ["x"], "val": [1]}).to_csv(authored, index=False)
    stage = {"id": "load", "description": "Load", "type": "input_data",
             "signature": {"form": "replaces", "produces": [
                 {"name": "name", "type": "str", "nullable": False},
                 {"name": "val", "type": "int", "nullable": False}]},
             "connector": {"kind": "file",
                           "params": {"path": str(authored), "format": "csv"}}}
    add_stage(proj, stage)
    vid = save_working_copy_as_version(proj.name, message="seed").version_id
    workspace.set_projects_dir(tmp_path)
    return proj, vid


def _manifest(vid: str, bindings: dict[str, Any], *, nested: bool = True) -> dict[str, Any]:
    base: dict[str, Any] = {"run_id": "20260806T000000", "workflow_version": vid}
    return {**base, "parameters": {"run_bindings": bindings}} if nested \
        else {**base, "run_bindings": bindings}


def _load_stage(workflow) -> Any:
    return next(s for s in workflow.stages if s.id == "load")


def _connector_path(workflow) -> str:
    return _load_stage(workflow).connector.params.paths[0]


def test_a_bound_path_is_what_the_panel_shows(project, tmp_path):
    proj, vid = project
    bound = tmp_path / "bound.parquet"
    pd.DataFrame({"name": ["z"], "val": [9]}).to_parquet(bound, index=False)
    manifest = _manifest(vid, {"load": {"path": str(bound), "format": "parquet"}})

    workflow = run_service.load_run_workflow("demo", manifest)

    assert _connector_path(workflow) == str(bound)
    assert _load_stage(workflow).connector.params.format == "parquet"


def test_the_pinned_stage_def_carries_the_binding_too(project, tmp_path):
    proj, vid = project
    bound = tmp_path / "bound.parquet"
    pd.DataFrame({"name": ["z"], "val": [9]}).to_parquet(bound, index=False)
    manifest = _manifest(vid, {"load": {"path": str(bound), "format": "parquet"}})

    pinned = run_service.load_pinned_stage_def("demo", manifest, "load")

    assert pinned.error is None
    assert pinned.workflow_stage.stage.connector.params.paths == [str(bound)]


def test_a_legacy_flat_manifest_is_read_the_same_way(project, tmp_path):
    proj, vid = project
    bound = tmp_path / "bound.parquet"
    pd.DataFrame({"name": ["z"], "val": [9]}).to_parquet(bound, index=False)
    manifest = _manifest(vid, {"load": {"path": str(bound), "format": "parquet"}},
                         nested=False)

    assert _connector_path(run_service.load_run_workflow("demo", manifest)) == str(bound)


def test_an_unbound_run_still_shows_the_authored_path(project):
    proj, vid = project
    workflow = run_service.load_run_workflow("demo", _manifest(vid, {}))
    assert _connector_path(workflow) == str(proj / "authored.csv")


def test_bindings_that_do_not_fit_the_pinned_version_fail_loudly(project):
    proj, vid = project
    manifest = _manifest(vid, {"no_such_stage": {"path": "/tmp/x.csv"}})
    with pytest.raises(RunVersionUnresolvableError, match="do not fit its pinned version"):
        run_service.load_run_workflow("demo", manifest)

"""A frame handed to a run is recorded like one the run computed: it gets a stage
record, an output on disk, a validation report against its own declared schema, and a
digest — so what entered the run is as inspectable as what came out of it, and a run
that really executed the stage can be checked against it."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import app.services.workspace as workspace
from app.core.errors import SubsetRunError
from app.core.frames import read_frame_file
from app.models import Workflow
from app.models.stage import parse_stage
from app.runtime.executor import run_subset
from app.runtime.runner import execute_run
from app.services import project as project_service
from app.services import versioning
from app.services.workflow_test import run_workflow_test
from conftest import pinned_stages

PROJECT = "overwritten_frames"
_COLUMNS = [
    {"name": "name", "type": "str", "nullable": True},
    {"name": "val", "type": "int", "nullable": True},
]


def _stages(data_path: Path) -> list[dict]:
    return [
        {
            "id": "load", "description": "Load rows", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": str(data_path), "format": "csv"}},
            "signature": {"form": "replaces", "produces": _COLUMNS},
        },
        {
            "id": "classify", "description": "Classify", "type": "python_row_function",
            "inputs": [{"id": "load", "schema": {"columns": _COLUMNS}}],
            "function": {"kind": "inline",
                         "code": 'def transform(row):\n    return {**row, "label": "x"}\n'},
            "signature": {
                "form": "extends",
                "reads": [{"input": "load", "columns": _COLUMNS}],
                "adds": [{"name": "label", "type": "str", "nullable": True}],
            },
        },
    ]


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    pdir = tmp_path / PROJECT
    (pdir / "compiled").mkdir(parents=True)
    data = pdir / "rows.csv"
    pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}).to_csv(data, index=False)
    for index, stage in enumerate(_stages(data), start=1):
        (pdir / "compiled" / f"{index:02d}_{stage['id']}.json").write_text(
            json.dumps(stage), encoding="utf-8")
    workspace.set_projects_dir(tmp_path)
    version_id = project_service.save_working_copy_as_version(
        pdir, message="v1", reviewer="test").version_id
    versioning.publish_version(pdir, version_id, reviewer="test")
    return pdir


def _manifest(project_dir: Path, run_id: str) -> dict:
    return json.loads(
        (project_dir / "runs" / run_id / "manifest.json").read_text(encoding="utf-8"))


def _record(manifest: dict, stage_id: str) -> dict:
    return next(r for r in manifest["stage_records"] if r["stage_id"] == stage_id)


def test_a_workflow_test_loads_its_own_data_and_overwrites_nothing(project: Path) -> None:
    """The default: every stage runs, sources included, so there is nothing to hand in."""
    run_id = run_workflow_test(PROJECT, limit=2, offset=0)["run_id"]
    records = _manifest(project, run_id)["stage_records"]

    assert [(r["stage_id"], r["status"]) for r in records] == [("load", "ok"), ("classify", "ok")]


def test_the_overwritten_rows_are_written_to_the_run(project: Path) -> None:
    run_id = run_workflow_test(PROJECT, limit=2, offset=0, stage_ids=["classify"])["run_id"]
    record = _record(_manifest(project, run_id), "load")

    assert record["status"] == "overwritten"
    assert record["output_row_count"] == 2
    written = read_frame_file(project / "runs" / run_id / record["output_path"])
    assert list(written["name"]) == ["a", "b"]


def test_the_overwritten_rows_are_checked_against_the_stages_own_schema(project: Path) -> None:
    """The consumer's input check covers only what IT reads; this is the producer's."""
    run_id = run_workflow_test(PROJECT, limit=2, offset=0, stage_ids=["classify"])["run_id"]
    report = _record(_manifest(project, run_id), "load")["output_validation_report"]

    assert report["phase"] == "output"
    assert report["ok"] is True
    assert report["rows"] == 2


def test_the_record_says_which_file_the_rows_came_from(project: Path) -> None:
    run_id = run_workflow_test(
        PROJECT, limit=1, offset=1, stage_ids=["classify"])["run_id"]
    manifest = _manifest(project, run_id)
    overwritten_by = _record(manifest, "load")["overwritten_by"]

    assert overwritten_by["origin"] == "source_file"
    assert overwritten_by["path"].endswith("rows.csv")
    assert len(overwritten_by["sha256"]) == 64
    # The window is legible as a window: two rows were there, one was taken.
    assert overwritten_by["rows_available"] == 2
    assert (overwritten_by["limit"], overwritten_by["offset"]) == (1, 1)
    # And the file lands where every run records the files it read.
    assert manifest["input_bindings"]["load"]["path"].endswith("rows.csv")


def test_an_overwritten_frame_digests_the_same_as_one_the_run_computed(project: Path) -> None:
    """The point of the digest: here the two agree, so the override checked out."""
    overwritten = _record(
        _manifest(project, run_workflow_test(
            PROJECT, limit=2, offset=0, stage_ids=["classify"])["run_id"]), "load")
    production_run = str(execute_run(project, project, *pinned_stages(project))["run_id"])
    computed = _record(_manifest(project, production_run), "load")

    assert computed["status"] == "ok"
    assert overwritten["content_digest"] == computed["content_digest"]


def test_a_frame_the_caller_handed_in_says_its_origin_is_unrecorded(tmp_path: Path) -> None:
    """It cannot know, so it says so rather than naming a file it never read."""
    stages = [parse_stage(spec) for spec in _stages(tmp_path / "unread.csv")]
    run_dir = tmp_path / "runs" / "subset1"
    run_subset(
        Workflow(stages=stages),
        injected_outputs={"load": pd.DataFrame({"name": ["a"], "val": [1]})},
        stage_ids=["classify"], run_dir=run_dir, repo_root=tmp_path,
    )
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    record = _record(manifest, "load")
    assert record["status"] == "overwritten"
    assert record["overwritten_by"] == {"origin": "caller"}
    assert manifest["input_bindings"] == {}


def test_an_overwritten_frame_that_breaks_the_schema_blocks_the_run(tmp_path: Path) -> None:
    """Same path a computed output takes, so it errors and its downstream never runs."""
    stages = [parse_stage(spec) for spec in _stages(tmp_path / "unread.csv")]
    run_dir = tmp_path / "runs" / "subset2"
    with pytest.raises(SubsetRunError, match="load"):
        run_subset(
            Workflow(stages=stages),
            injected_outputs={"load": pd.DataFrame({"name": ["a"], "val": ["nope"]})},
            stage_ids=["classify"], run_dir=run_dir, repo_root=tmp_path,
        )
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    load = _record(manifest, "load")
    assert load["status"] == "error"
    assert load["error"]["type"] == "OutputSchemaViolation"
    assert any(i["column"] == "val" for i in load["output_validation_report"]["issues"])
    # The frame is still on disk, and the consumer never saw it.
    assert (run_dir / load["output_path"]).exists()
    assert _record(manifest, "classify")["status"] == "pending"

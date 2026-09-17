"""A captured run restored into a clean workspace, and what a restore refuses."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
import zipfile

import pandas as pd
import pytest

from app.core.agent.usage import LlmUsage
from app.models.captured_run import CAPTURED_ARCHIVE, CAPTURED_INPUTS, CAPTURED_RECORD
from app.services import methodology, workspace
from app.services import project as project_service
from app.services import run as run_service
from app.services.errors import RunRestoreRefused
from app.services.project import export_project_archive
from app.services.run_restore import restore_run
from scope_fixture import review_tail, stage_specs, write_inputs
from scripts.run_capture import capture_run
from stage_seed import set_stages

_COLUMNS = [{"name": "x", "type": "int", "nullable": True}]


def _load_stage(data: Path) -> dict:
    return {
        "id": "load", "type": "input_data", "description": "Loads the rows to judge.",
        "connector": {"kind": "file",
                      "params": {"paths": [str(data)], "format": "csv"}},
        "signature": {"form": "replaces", "produces": _COLUMNS},
    }


def _judge_stage() -> dict:
    return {
        "id": "judge", "type": "llm_transform", "description": "Judges each row.",
        "inputs": [{"id": "load"}],
        "signature": {"form": "extends",
                      "reads": [{"input": "load", "columns": _COLUMNS}],
                      "adds": [{"name": "verdict", "type": "str", "nullable": True}]},
        "llm": {"prompt_instructions": "judge it", "prompt_data_template": "{x}",
                "batch_size": 1},
    }


class _ModelCallCount:
    def __init__(self) -> None:
        self.calls = 0

    def answer(self, stage_id, llm, row, reply_model, usage_out):
        self.calls += 1
        usage_out.append(
            LlmUsage(input_tokens=10, output_tokens=5, cost_usd=0.25, calls=1))
        return {"verdict": f"v{row['x']}"}


@pytest.fixture
def model(monkeypatch) -> _ModelCallCount:
    counter = _ModelCallCount()
    monkeypatch.setattr(
        "app.runtime.stages.llm_transform.call_llm", counter.answer)
    return counter


def _seed_a_judging_project(projects_root: Path) -> str:
    project_id = project_service.create_project(
        "Rows A Model Judged", "Two rows, judged one at a time.",
        model="sonnet", source="test").id
    data = projects_root / project_id / "rows.csv"
    data.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"x": [1, 2]}).to_csv(data, index=False)
    set_stages(project_id, [_load_stage(data), _judge_stage()])
    project_service.save_working_copy_as_version(project_id, message="v1")
    return project_id


def _empty_the_workspace(tmp_path: Path, monkeypatch, name: str) -> None:
    """A second workspace with its own store, frames, files and projects directory."""
    from app.core.frames import FrameStore, configure_frame_store
    from app.core.persistence import configure_store
    from app.core.sqlite_store import SqliteKvStore

    configure_store(SqliteKvStore(":memory:"))
    configure_frame_store(FrameStore(tmp_path / name / "frames"))
    monkeypatch.setenv("CARBON_PAPER_FILES_ROOT", str(tmp_path / name / "files"))
    workspace.set_projects_dir(tmp_path / name / "examples")


def test_a_captured_run_restores_the_same_rows_without_calling_the_model(
    projects_root, tmp_path, monkeypatch, model
):
    project_id = _seed_a_judging_project(projects_root)
    run_id = str(run_service.execute(project_id)["run_id"])
    was_judged = run_service.read_stage_output(project_id, run_id, "judge")
    into = tmp_path / "capture"
    capture_run(project_id, run_id, into)
    paid_for = model.calls
    assert paid_for == 2 and list(was_judged["verdict"]) == ["v1", "v2"]
    _empty_the_workspace(tmp_path, monkeypatch, "elsewhere")

    restored = restore_run(into)

    assert model.calls == paid_for
    pd.testing.assert_frame_equal(
        run_service.read_stage_output(restored.project_id, restored.run_id, "judge"),
        was_judged)
    assert restored.project_id != project_id


def test_a_restore_reads_the_files_it_was_handed_rather_than_the_recorded_paths(
    projects_root, tmp_path, monkeypatch, model
):
    project_id = _seed_a_judging_project(projects_root)
    run_id = str(run_service.execute(project_id)["run_id"])
    into = tmp_path / "capture"
    capture_run(project_id, run_id, into)
    _empty_the_workspace(tmp_path, monkeypatch, "elsewhere")
    (projects_root / project_id / "rows.csv").unlink()

    restored = restore_run(into)

    assert len(run_service.read_stage_output(restored.project_id, restored.run_id,
                                            "load")) == 2


def test_an_input_file_tampered_with_since_the_capture_is_refused(
    projects_root, tmp_path, monkeypatch, model
):
    project_id = _seed_a_judging_project(projects_root)
    run_id = str(run_service.execute(project_id)["run_id"])
    into = tmp_path / "capture"
    capture_run(project_id, run_id, into)
    copied = into / CAPTURED_INPUTS / "load" / "rows.csv"
    # Same length, so the byte count still matches and only the digest can catch it.
    copied.write_text(copied.read_text(encoding="utf-8").replace("1", "7", 1),
                      encoding="utf-8")
    _empty_the_workspace(tmp_path, monkeypatch, "elsewhere")

    with pytest.raises(RunRestoreRefused, match="hashes to"):
        restore_run(into)


def test_an_archive_carrying_no_stage_cache_is_refused(
    projects_root, tmp_path, monkeypatch, model
):
    project_id = _seed_a_judging_project(projects_root)
    run_id = str(run_service.execute(project_id)["run_id"])
    into = tmp_path / "capture"
    capture_run(project_id, run_id, into)
    _drop_the_cache_half(into / CAPTURED_ARCHIVE)
    _empty_the_workspace(tmp_path, monkeypatch, "elsewhere")

    with pytest.raises(RunRestoreRefused, match="not a stage-cache export"):
        restore_run(into)


def _drop_the_cache_half(archive: Path) -> None:
    """Leaves the project bundle alone, so what is refused is the missing cache."""
    kept = BytesIO()
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(kept, "w") as rebuilt:
        for name in source.namelist():
            if name != "manifest.json":
                rebuilt.writestr(name, source.read(name))
    archive.write_bytes(kept.getvalue())


def test_a_workflow_that_queues_rows_for_review_is_refused_before_it_runs(
    projects_root, tmp_path
):
    into = tmp_path / "capture"
    into.mkdir(parents=True, exist_ok=True)
    (into / CAPTURED_ARCHIVE).write_bytes(_a_reviewing_project(projects_root))
    (into / CAPTURED_RECORD).write_text(
        '{"project_name": "Grants Awaiting Review", "run_id": "r1", "inputs": []}',
        encoding="utf-8")

    with pytest.raises(RunRestoreRefused, match="review_totals"):
        restore_run(into)


def _a_reviewing_project(projects_root: Path) -> bytes:
    project_id = project_service.create_project(
        "Grants Awaiting Review", "Twelve grants, one of them signed off by hand.",
        model="sonnet", source="test").id
    data = projects_root / project_id / "data"
    write_inputs(data)
    set_stages(project_id, [*stage_specs(data), *review_tail()])
    project_service.save_working_copy_as_version(project_id, message="v1")
    return export_project_archive(project_id)


def test_a_capture_directory_with_no_record_is_refused(tmp_path):
    with pytest.raises(RunRestoreRefused, match=CAPTURED_RECORD):
        restore_run(tmp_path / "nothing-here")


def test_a_record_that_does_not_validate_is_refused(tmp_path):
    into = tmp_path / "capture"
    into.mkdir()
    (into / CAPTURED_RECORD).write_text('{"run_id": "r1"}', encoding="utf-8")

    with pytest.raises(RunRestoreRefused, match="not a captured run"):
        restore_run(into)


def test_a_capture_directory_with_no_archive_is_refused(tmp_path):
    into = tmp_path / "capture"
    into.mkdir()
    (into / CAPTURED_RECORD).write_text(
        '{"project_name": "p", "run_id": "r1", "inputs": []}', encoding="utf-8")

    with pytest.raises(RunRestoreRefused, match=CAPTURED_ARCHIVE):
        restore_run(into)


def test_the_restored_project_carries_the_captured_document(
    projects_root, tmp_path, monkeypatch, model
):
    project_id = _seed_a_judging_project(projects_root)
    run_id = str(run_service.execute(project_id)["run_id"])
    into = tmp_path / "capture"
    capture_run(project_id, run_id, into)
    _empty_the_workspace(tmp_path, monkeypatch, "elsewhere")

    restored = restore_run(into)

    assert methodology.read_methodology(restored.project_id) == (
        "Two rows, judged one at a time.")


def test_a_refused_tampered_input_leaves_no_project_behind(
    projects_root, tmp_path, monkeypatch, model
):
    project_id = _seed_a_judging_project(projects_root)
    run_id = str(run_service.execute(project_id)["run_id"])
    into = tmp_path / "capture"
    capture_run(project_id, run_id, into)
    copied = into / CAPTURED_INPUTS / "load" / "rows.csv"
    copied.write_text(copied.read_text(encoding="utf-8").replace("1", "7", 1),
                      encoding="utf-8")
    _empty_the_workspace(tmp_path, monkeypatch, "elsewhere")
    before = project_service.list_projects()

    with pytest.raises(RunRestoreRefused, match="hashes to"):
        restore_run(into)

    assert project_service.list_projects() == before


def test_a_refused_review_queue_leaves_no_project_behind(projects_root, tmp_path):
    into = tmp_path / "capture"
    into.mkdir(parents=True, exist_ok=True)
    (into / CAPTURED_ARCHIVE).write_bytes(_a_reviewing_project(projects_root))
    (into / CAPTURED_RECORD).write_text(
        '{"project_name": "Grants Awaiting Review", "run_id": "r1", "inputs": []}',
        encoding="utf-8")
    before = project_service.list_projects()

    with pytest.raises(RunRestoreRefused, match="review_totals"):
        restore_run(into)

    assert project_service.list_projects() == before

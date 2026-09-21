"""A captured run restored into a clean workspace, and what a restore refuses."""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import zipfile

import pandas as pd
import pytest

from app.core.agent.usage import LlmUsage
from app.core.files import save_upload
from app.models import Workflow, WorkflowStage
from app.models.stages.human_review_queue import ReviewVerdict, resolve_queue_config
from app.services import methodology, review, uploads, versioning, workspace
from app.services import project as project_service
from app.services import run as run_service
from app.web import loading
from app.services.errors import RunRestoreRefused
from app.services.run_restore import (
    CAPTURED_ARCHIVE,
    CAPTURED_INPUTS,
    CAPTURED_RECORD,
    restore_run,
)
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


def test_a_capture_with_no_run_record_is_refused_and_leaves_no_project_behind(
    projects_root, tmp_path, monkeypatch, model
):
    project_id = _seed_a_judging_project(projects_root)
    run_id = str(run_service.execute(project_id)["run_id"])
    into = tmp_path / "capture"
    capture_run(project_id, run_id, into)
    (into / CAPTURED_RECORD).unlink()
    _empty_the_workspace(tmp_path, monkeypatch, "elsewhere")
    before = project_service.list_projects()

    with pytest.raises(RunRestoreRefused, match=CAPTURED_RECORD):
        restore_run(into)

    assert project_service.list_projects() == before


def test_a_capture_directory_with_no_archive_is_refused(tmp_path):
    with pytest.raises(RunRestoreRefused, match=CAPTURED_ARCHIVE):
        restore_run(tmp_path / "nothing-here")


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


def test_an_input_directory_naming_a_stage_the_workflow_lacks_is_refused(
    projects_root, tmp_path, monkeypatch, model
):
    project_id = _seed_a_judging_project(projects_root)
    run_id = str(run_service.execute(project_id)["run_id"])
    into = tmp_path / "capture"
    capture_run(project_id, run_id, into)
    stale = into / CAPTURED_INPUTS / "load_west"
    stale.mkdir()
    (stale / "rows.csv").write_text("x\n7\n", encoding="utf-8")
    _empty_the_workspace(tmp_path, monkeypatch, "elsewhere")
    before = project_service.list_projects()

    with pytest.raises(RunRestoreRefused, match="load_west"):
        restore_run(into)

    assert project_service.list_projects() == before


# ── the seeded project, whose queue halts a run for a person ─────────────────

_SEED_DIR = Path(__file__).resolve().parents[1] / "app" / "seeds" / "data"
_SEED_BUNDLE = _SEED_DIR / "ai_lobbying_spend_2026.json"
_SEED_INPUTS = [_SEED_DIR / "lda_data_Q1_2026.xlsx", _SEED_DIR / "lda_data_Q2_2026.xlsx"]
_INPUT_STAGE = "input_filings"
_REVIEW_STAGE = "review_ai_spend"
_REPORTING_STAGE = "ai_spend_by_client"
# A restore that lost this window would read both quarters in full.
_WINDOW = 50
_SEEDED_BY = "seeded fixture decision, not reviewed by a person"


def test_a_reviewed_run_restores_through_its_queue_without_calling_the_model(
    projects_root, tmp_path, monkeypatch, model
):
    monkeypatch.setattr(
        run_service, "_run_in_background", lambda target, *args: target(*args))
    project_id = project_service.import_bundle_file(_SEED_BUNDLE).project_id
    run_id = _run_the_seeded_window(project_id)
    approved = _approve_every_queued_row(project_id, run_id)
    run_service.resume(project_id, run_id)
    was_reported = run_service.read_stage_output(project_id, run_id, _REPORTING_STAGE)
    assert run_service.read_run_manifest(project_id, run_id).status == "ok"
    into = tmp_path / "capture"
    capture_run(project_id, run_id, into)
    assert model.calls == 0
    _empty_the_workspace(tmp_path, monkeypatch, "elsewhere")

    restored = restore_run(into)

    assert model.calls == 0
    assert run_service.read_run_manifest(
        restored.project_id, restored.run_id).parameters.limits == {_INPUT_STAGE: _WINDOW}
    re_recorded = review.find_decisions_oldest_first(restored.project_id)
    assert {one.reviewer for one in re_recorded} == {_SEEDED_BY}
    assert len(re_recorded) == approved
    pd.testing.assert_frame_equal(
        run_service.read_stage_output(
            restored.project_id, restored.run_id, _REPORTING_STAGE),
        was_reported)


def _run_the_seeded_window(project_id: str) -> str:
    result = run_service.execute(
        project_id, bindings=_bind_the_seed_filings(project_id),
        limits={_INPUT_STAGE: _WINDOW})
    assert result["status"] == "awaiting_review"
    return str(result["run_id"])


def _bind_the_seed_filings(project_id: str) -> dict:
    stored = []
    for path in _SEED_INPUTS:
        with path.open("rb") as handle:
            stored.append(save_upload(path.name, handle, project_id=project_id).id)
    return {_INPUT_STAGE: uploads.resolve_files_binding(project_id, stored)}


def _approve_every_queued_row(project_id: str, run_id: str) -> int:
    """What the decide route does, minus the form: the received value back is an approve."""
    queued = _read_the_queue(project_id, run_id)
    for fingerprint, row in queued.rows_by_fingerprint.items():
        review.record_decision(
            project_id=project_id, stage=queued.stage,
            stage_fingerprint=queued.stage_fingerprint,
            input_fingerprint=fingerprint, frozen_row=row,
            verdict=ReviewVerdict.approve,
            reviewed_values={target: row[source]
                             for source, target in queued.reviewed_columns.items()},
            review_notes=None, reviewer=_SEEDED_BY,
            reviewed_at="2026-09-21T09:00:00",
            workflow_version_id=run_service.read_pinned_version(project_id, run_id),
            workflow_run_id=run_id,
        )
    assert queued.rows_by_fingerprint
    return len(queued.rows_by_fingerprint)


@dataclass(frozen=True)
class _QueuedRows:
    stage: WorkflowStage
    stage_fingerprint: str
    # Source column in the queued row -> the column the decision lands in.
    reviewed_columns: dict[str, str]
    rows_by_fingerprint: dict[str, dict]


def _read_the_queue(project_id: str, run_id: str) -> _QueuedRows:
    workflow = Workflow(stages=versioning.load_version_stages(
        project_id, run_service.read_pinned_version(project_id, run_id)))
    stage = workflow.find_workflow_stage(_REVIEW_STAGE)
    queue = resolve_queue_config(stage.stage)
    assert queue is not None
    fingerprints = loading.load_queue_fingerprints(project_id, run_id, _REVIEW_STAGE)
    rows = loading.queue_snapshot_rows(project_id, run_id, _REVIEW_STAGE)
    assert fingerprints is not None and rows is not None
    return _QueuedRows(
        stage=stage, stage_fingerprint=fingerprints.stage_fingerprint,
        reviewed_columns=dict(queue.reviewed_columns),
        rows_by_fingerprint=dict(
            zip(fingerprints.input_fingerprints, rows, strict=True)),
    )

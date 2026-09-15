from __future__ import annotations

import hashlib
import subprocess
import zipfile
from collections.abc import Callable
from datetime import datetime
from io import BytesIO
from pathlib import Path

import pytest

from app.core.files import delete_file, list_project_files
from app.core.run_status import RunStatus
from app.models.records.workflow_output import WorkflowOutput
from app.runtime.cancellation import request_cancel
from app.services import project
from app.services.project import export_project_archive
from app.services.run import execute, read_run_manifest, start_run
from app.services.workflow_test import run_workflow_test
from evals.runs.capture import CaptureRefused, capture_run
from evals.runs.recipe import (
    ARCHIVE_FILE,
    RECIPE_FILE,
    RecipeFigure,
    RecipeInput,
    SuppliedLocation,
    read_recipe,
)
from tiny_run import (
    LOAD_STAGE,
    REVIEW_STAGE,
    ROWS_FILENAME,
    TINY_ROWS,
    TOTALS_STAGE,
    bind_uploaded_rows,
    create_reviewed_run_past_a_queue_that_caches,
    create_reviewed_run_past_a_queue_that_does_not_cache,
    create_run_halted_at_a_queue_that_does_not_cache,
    create_tiny_project,
    create_tiny_project_naming_rows_at,
    create_tiny_run,
    create_tiny_run_publishing_a_slug_twice,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SAVED = "saved"
_ROWS_MISSING_A_REQUIRED_AMOUNT = b"name,amount\nacme,\n"


def test_capture_records_inputs_status_limits_and_every_figure(tmp_path: Path) -> None:
    # Another run publishes the same slugs; only the captured run's figures may reach its recipe.
    create_tiny_run()
    run = create_tiny_run(limits={LOAD_STAGE: 2})
    recipe = read_recipe(_capture(tmp_path, run.project_id, run.run_id))
    assert recipe.inputs == [
        RecipeInput(
            stage_id=LOAD_STAGE,
            filename=ROWS_FILENAME,
            bytes=len(TINY_ROWS),
            sha256=hashlib.sha256(TINY_ROWS).hexdigest(),
            at=SuppliedLocation(),
        )
    ]
    assert recipe.ends == RunStatus.OK
    assert recipe.limits == {LOAD_STAGE: 2}
    assert recipe.figures == [
        RecipeFigure(slug="amount-total", stage_id=TOTALS_STAGE, value=7, claimable=False),
        RecipeFigure(slug="largest-amount", stage_id=TOTALS_STAGE, value=4, claimable=False),
        RecipeFigure(slug="row-count", stage_id=TOTALS_STAGE, value=2, claimable=True),
    ]


def test_capture_sorts_figures_by_slug_whatever_order_the_store_lists_them_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = create_tiny_run()
    published = [output for output in WorkflowOutput.list() if output.citation.run_id == run.run_id]
    in_reverse_slug_order = sorted(published, key=lambda output: output.slug, reverse=True)
    monkeypatch.setattr(WorkflowOutput, "list", lambda prefix="": in_reverse_slug_order)
    recipe = read_recipe(_capture(tmp_path, run.project_id, run.run_id))
    assert [figure.slug for figure in recipe.figures] == [
        "amount-total",
        "largest-amount",
        "row-count",
    ]


def test_capture_records_the_row_offsets_of_the_run(tmp_path: Path) -> None:
    run = create_tiny_run(offsets={LOAD_STAGE: 1})
    assert read_recipe(_capture(tmp_path, run.project_id, run.run_id)).offsets == {LOAD_STAGE: 1}


def test_capture_records_the_project_version_and_commit_it_came_from_and_when(
    tmp_path: Path,
) -> None:
    run = create_tiny_run()
    before = datetime.now().replace(microsecond=0)
    captured = read_recipe(_capture(tmp_path, run.project_id, run.run_id)).captured
    after = datetime.now()
    assert (captured.project_id, captured.workflow_version, captured.code_commit) == (
        run.project_id,
        read_run_manifest(run.project_id, run.run_id).workflow_version,
        _read_head_commit(_REPO_ROOT),
    )
    captured_at = datetime.fromisoformat(captured.captured_at)
    assert before <= captured_at <= after
    assert captured.captured_at == captured_at.isoformat(timespec="seconds")


def test_capture_writes_the_archive_beside_the_recipe(tmp_path: Path) -> None:
    run = create_tiny_run()
    saved = _capture(tmp_path, run.project_id, run.run_id)
    assert saved == tmp_path / _SAVED / run.run_id
    assert {path.name for path in saved.iterdir()} == {ARCHIVE_FILE, RECIPE_FILE}
    assert _read_members((saved / ARCHIVE_FILE).read_bytes()) == _read_members(
        export_project_archive(run.project_id)
    )
    assert read_recipe(saved).workflow_run_id == run.run_id


def test_capture_refuses_a_run_still_going(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_id = create_tiny_project()
    monkeypatch.setattr("app.services.run._run_in_background", lambda target, *args: None)
    run_id = start_run(project_id, bindings=bind_uploaded_rows(project_id, TINY_ROWS))
    with pytest.raises(CaptureRefused, match="has status 'running'"):
        _capture(tmp_path, project_id, run_id)
    assert not (tmp_path / _SAVED).exists()


def test_capture_refuses_a_cancelled_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_id = create_tiny_project()
    monkeypatch.setattr(
        "app.services.run._run_in_background", _cancel_before_executing(project_id)
    )
    run_id = start_run(project_id, bindings=bind_uploaded_rows(project_id, TINY_ROWS))
    with pytest.raises(CaptureRefused, match="has status 'cancelled'"):
        _capture(tmp_path, project_id, run_id)
    assert not (tmp_path / _SAVED).exists()


def test_capture_refuses_a_run_that_ended_in_errors(tmp_path: Path) -> None:
    project_id = create_tiny_project()
    bindings = bind_uploaded_rows(project_id, _ROWS_MISSING_A_REQUIRED_AMOUNT)
    run_id = str(execute(project_id, bindings=bindings)["run_id"])
    with pytest.raises(CaptureRefused, match="has status 'errors'"):
        _capture(tmp_path, project_id, run_id)
    assert not (tmp_path / _SAVED).exists()


def test_capture_refuses_a_test_run(tmp_path: Path) -> None:
    project_id = create_tiny_project_naming_rows_at(_write_rows(tmp_path))
    run_id = str(run_workflow_test(project_id)["run_id"])
    with pytest.raises(CaptureRefused, match="is a test run"):
        _capture(tmp_path, project_id, run_id)
    assert not (tmp_path / _SAVED).exists()


def test_capture_refuses_a_run_whose_version_is_not_the_latest(tmp_path: Path) -> None:
    run = create_tiny_run()
    ran = read_run_manifest(run.project_id, run.run_id).workflow_version
    later = project.save_working_copy_as_version(run.project_id, message="Save a later version")
    with pytest.raises(CaptureRefused, match="the archive exports the latest") as refused:
        _capture(tmp_path, run.project_id, run.run_id)
    assert f"'{ran}'" in str(refused.value)
    assert f"'{later.version_id}'" in str(refused.value)
    assert not (tmp_path / _SAVED).exists()


def test_capture_refuses_an_input_the_workflow_names_by_path(tmp_path: Path) -> None:
    rows = _write_rows(tmp_path)
    project_id = create_tiny_project_naming_rows_at(rows)
    run_id = str(execute(project_id)["run_id"])
    with pytest.raises(CaptureRefused, match="a path the workflow names") as refused:
        _capture(tmp_path, project_id, run_id)
    assert str(rows) in str(refused.value)
    assert not (tmp_path / _SAVED).exists()


def test_capture_refuses_an_input_bound_to_a_file_the_store_does_not_hold(tmp_path: Path) -> None:
    rows = _write_rows(tmp_path)
    project_id = create_tiny_project()
    bindings = {LOAD_STAGE: {"paths": [str(rows)], "format": "csv"}}
    run_id = str(execute(project_id, bindings=bindings)["run_id"])
    with pytest.raises(CaptureRefused, match="not a file uploaded to the project") as refused:
        _capture(tmp_path, project_id, run_id)
    assert f"'{ROWS_FILENAME}'" in str(refused.value)
    assert not (tmp_path / _SAVED).exists()


def test_capture_refuses_an_input_whose_file_record_is_gone(tmp_path: Path) -> None:
    run = create_tiny_run()
    [stored] = list_project_files(run.project_id)
    delete_file(run.project_id, stored.id)
    with pytest.raises(CaptureRefused, match="whose record is gone") as refused:
        _capture(tmp_path, run.project_id, run.run_id)
    assert f"'{ROWS_FILENAME}'" in str(refused.value)
    assert not (tmp_path / _SAVED).exists()


def test_capture_refuses_a_run_that_publishes_one_slug_twice(tmp_path: Path) -> None:
    run = create_tiny_run_publishing_a_slug_twice()
    with pytest.raises(CaptureRefused, match="more than one figure") as refused:
        _capture(tmp_path, run.project_id, run.run_id)
    assert "'amount-total'" in str(refused.value)
    assert not (tmp_path / _SAVED).exists()


def test_capture_refuses_a_run_past_a_review_queue_that_does_not_cache(tmp_path: Path) -> None:
    run = create_reviewed_run_past_a_queue_that_does_not_cache()
    with pytest.raises(CaptureRefused, match="a saved run carries no review decisions") as refused:
        _capture(tmp_path, run.project_id, run.run_id)
    assert f"'{REVIEW_STAGE}'" in str(refused.value)
    assert not (tmp_path / _SAVED).exists()


def test_capture_records_a_run_past_a_review_queue_that_caches(tmp_path: Path) -> None:
    run = create_reviewed_run_past_a_queue_that_caches()
    assert read_recipe(_capture(tmp_path, run.project_id, run.run_id)).ends == RunStatus.OK


def test_capture_records_a_run_halted_at_a_review_queue_that_does_not_cache(
    tmp_path: Path,
) -> None:
    run = create_run_halted_at_a_queue_that_does_not_cache()
    recipe = read_recipe(_capture(tmp_path, run.project_id, run.run_id))
    assert recipe.ends == RunStatus.AWAITING_REVIEW


def test_capture_refuses_to_overwrite_a_saved_run_unless_replacing(tmp_path: Path) -> None:
    run = create_tiny_run()
    saved = _capture(tmp_path, run.project_id, run.run_id)
    for name in (ARCHIVE_FILE, RECIPE_FILE):
        (saved / name).write_bytes(b"stale")
    with pytest.raises(CaptureRefused, match="already holds a saved run"):
        _capture(tmp_path, run.project_id, run.run_id)
    assert [(saved / name).read_bytes() for name in (ARCHIVE_FILE, RECIPE_FILE)] == [b"stale"] * 2
    _capture(tmp_path, run.project_id, run.run_id, replace=True)
    assert read_recipe(saved).workflow_run_id == run.run_id
    assert _read_members((saved / ARCHIVE_FILE).read_bytes()) == _read_members(
        export_project_archive(run.project_id)
    )


def test_a_git_failure_stops_capture_before_anything_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = create_tiny_run()
    outside_any_repository = tmp_path / "no_repository"
    outside_any_repository.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    with pytest.raises(subprocess.CalledProcessError):
        capture_run(
            run.project_id,
            run.run_id,
            tmp_path / _SAVED,
            repo_root=outside_any_repository,
            replace=False,
        )
    assert not (tmp_path / _SAVED).exists()


def _capture(tmp_path: Path, project_id: str, run_id: str, *, replace: bool = False) -> Path:
    return capture_run(project_id, run_id, tmp_path / _SAVED, repo_root=_REPO_ROOT, replace=replace)


def _cancel_before_executing(project_id: str) -> Callable[..., None]:
    def execute_after_a_cancel(
        execute_prepared: Callable[[dict[str, object]], object], prepared: dict[str, object]
    ) -> None:
        request_cancel(project_id, str(prepared["run_id"]))
        execute_prepared(prepared)

    return execute_after_a_cancel


def _write_rows(tmp_path: Path) -> Path:
    rows = tmp_path / ROWS_FILENAME
    rows.write_bytes(TINY_ROWS)
    return rows


def _read_head_commit(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, check=True
    )
    return completed.stdout.decode("utf-8").strip()


def _read_members(archive: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(BytesIO(archive)) as opened:
        return {name: opened.read(name) for name in opened.namelist()}

from __future__ import annotations

import hashlib
import zipfile
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

import pytest

from app.core.agent.usage import LlmUsage
from app.core.errors import LLMError
from app.core.json_types import JsonScalar
from app.core.run_status import RunStatus, StageStatus
from app.models import Stage
from app.models.citations import StageOutputCellCitation
from app.models.records.run_manifest import RunManifest
from app.models.records.workflow_output import WorkflowOutput
from app.models.run_manifest import StageRecord, read_input_bindings
from app.models.stages.human_review_queue import HumanReviewQueueStage
from app.models.stages.stage_base import StageType
from app.runtime import options
from app.runtime.options import require_agent_backend
from app.services import project
from app.services.loader import save_stages
from app.services.project import export_project_archive
from app.services.run import list_every_run_entry, read_run_manifest
from app.services.versioning import find_latest_version_id, load_version_stages
from evals.runs.capture import capture_run
from evals.runs.rebuild import (
    RebuiltRun,
    RebuiltRunRefused,
    RunRefused,
    find_model_spend,
    rebuild_run,
)
from evals.runs.recipe import (
    ARCHIVE_FILE,
    RecipeFigure,
    RecipeInput,
    RepoPathLocation,
    SuppliedLocation,
    read_recipe,
    write_recipe,
)
from evals.runs.workspace import configure_throwaway_workspace
from tiny_run import (
    LOAD_STAGE,
    MODEL_STAGE,
    REVIEW_STAGE,
    ROWS_FILENAME,
    TINY_ROWS,
    TOTALS_STAGE,
    TinyRun,
    create_reviewed_run_past_a_queue_that_caches,
    create_run_with_a_model_stage_that_does_not_cache,
    create_tiny_run,
    create_tiny_run_publishing_a_slug_twice,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SAME_SIZE_OTHER_ROWS = b"name,amount\nacme,3\nglobex,4\ninitech,6\n"
_MORE_ROWS = TINY_ROWS + b"hooli,6\n"
_TINY_ROW_COUNT = len(TINY_ROWS.splitlines()) - 1


def test_a_rebuilt_run_reproduces_the_captured_status_and_figures(tmp_path: Path) -> None:
    saved = _capture(tmp_path, create_tiny_run(limits={LOAD_STAGE: 1}, offsets={LOAD_STAGE: 1}))
    rebuilt = _rebuild(tmp_path, saved, _write_inputs(tmp_path, {ROWS_FILENAME: TINY_ROWS}))
    recipe = read_recipe(saved)
    assert recipe.ends == RunStatus.OK
    assert read_run_manifest(rebuilt.project_id, rebuilt.run_id).status == RunStatus.OK
    # The window keeps one row, globex with amount 4: a count of 1, and 4 as sum and maximum.
    worked_out = [
        ("amount-total", "amount_total", 4),
        ("largest-amount", "largest_amount", 4),
        ("row-count", "row_count", 1),
    ]
    assert [(figure.slug, figure.value) for figure in recipe.figures] == [
        (slug, value) for slug, _column, value in worked_out
    ]
    published = [(output.slug, output.citation) for output in WorkflowOutput.list()]
    assert sorted(published, key=lambda pair: pair[0]) == [
        (slug, _cite_totals_cell(rebuilt.run_id, column, value))
        for slug, column, value in worked_out
    ]


def test_a_rebuilt_run_replays_the_review_decisions_its_archive_carries(tmp_path: Path) -> None:
    saved = _capture(tmp_path, create_reviewed_run_past_a_queue_that_caches())
    rebuilt = _rebuild(tmp_path, saved, _write_inputs(tmp_path, {ROWS_FILENAME: TINY_ROWS}))
    review = read_run_manifest(rebuilt.project_id, rebuilt.run_id).find_stage_record(REVIEW_STAGE)
    assert review is not None and review.cached_rows == _TINY_ROW_COUNT


def test_rebuild_refuses_an_archive_other_than_the_captured_one_before_importing_it(
    tmp_path: Path,
) -> None:
    saved = _capture(tmp_path, create_tiny_run())
    with zipfile.ZipFile(saved / ARCHIVE_FILE, "a") as archive:
        archive.writestr("added_after_capture.txt", b"not part of the capture")
    found = hashlib.sha256((saved / ARCHIVE_FILE).read_bytes()).hexdigest()
    inputs_dir = _write_inputs(tmp_path, {ROWS_FILENAME: TINY_ROWS})
    [reason] = _collect_refusal_reasons(tmp_path, saved, inputs_dir)
    assert read_recipe(saved).archive_sha256 in reason and found in reason
    assert project.list_projects() == []


@pytest.mark.parametrize(
    ("rows", "recorded_and_found"),
    [
        (
            _SAME_SIZE_OTHER_ROWS,
            (hashlib.sha256(TINY_ROWS).hexdigest(), hashlib.sha256(_SAME_SIZE_OTHER_ROWS).hexdigest()),
        ),
        (_MORE_ROWS, (f"{len(TINY_ROWS)} bytes", f"{len(_MORE_ROWS)} bytes")),
    ],
    ids=["same size, other content", "other size"],
)
def test_rebuild_refuses_an_input_whose_bytes_differ(
    tmp_path: Path, rows: bytes, recorded_and_found: tuple[str, str]
) -> None:
    saved = _capture(tmp_path, create_tiny_run())
    reasons = _collect_refusal_reasons(tmp_path, saved, _write_inputs(tmp_path, {ROWS_FILENAME: rows}))
    assert any(
        f"'{ROWS_FILENAME}'" in reason and all(value in reason for value in recorded_and_found)
        for reason in reasons
    ), reasons


def test_rebuild_refuses_a_supplied_input_it_cannot_find_naming_where_it_looked(
    tmp_path: Path,
) -> None:
    saved = _capture(tmp_path, create_tiny_run())
    inputs_dir = _write_inputs(tmp_path, {})
    # Named through '..', so only a reason showing the resolved path passes.
    given = tmp_path / "saved" / ".." / inputs_dir.name
    [reason] = _collect_refusal_reasons(tmp_path, saved, given)
    assert f"is not at {(inputs_dir / ROWS_FILENAME).resolve()}," in reason


def test_rebuild_refuses_a_supplied_input_when_given_no_inputs_folder(tmp_path: Path) -> None:
    saved = _capture(tmp_path, create_tiny_run())
    [reason] = _collect_refusal_reasons(tmp_path, saved, None)
    assert f"'{ROWS_FILENAME}'" in reason and "no inputs folder" in reason


@pytest.mark.parametrize(
    "name_the_file_at",
    [lambda outside: str(outside), lambda outside: f"../{outside.parent.name}/{outside.name}"],
    ids=["absolute", "through the parent folder"],
)
def test_rebuild_refuses_a_supplied_filename_that_leaves_the_inputs_folder(
    tmp_path: Path, name_the_file_at: Callable[[Path], str]
) -> None:
    saved = _capture(tmp_path, create_tiny_run())
    outside = tmp_path / "elsewhere" / ROWS_FILENAME
    outside.parent.mkdir()
    outside.write_bytes(TINY_ROWS)
    _update_recipe_inputs(saved, {"filename": name_the_file_at(outside)})
    [reason] = _collect_refusal_reasons(tmp_path, saved, _write_inputs(tmp_path, {}))
    assert f"resolves to {outside.resolve()}," in reason and "outside the inputs folder" in reason


def test_rebuild_refuses_a_repo_path_input_outside_the_repo_root(tmp_path: Path) -> None:
    saved = _capture(tmp_path, create_tiny_run())
    outside = _write_inputs(tmp_path, {ROWS_FILENAME: TINY_ROWS}) / ROWS_FILENAME
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _update_recipe_inputs(saved, {"at": RepoPathLocation(path=f"../inputs/{ROWS_FILENAME}")})
    configure_throwaway_workspace(tmp_path / "ws")
    with pytest.raises(RunRefused) as refused:
        rebuild_run(saved, repo_root=repo_root, inputs_dir=None)
    [reason] = refused.value.reasons
    assert str(outside.resolve()) in reason and str(repo_root.resolve()) in reason


def test_rebuild_reads_a_repo_path_input_from_under_the_repo_root(tmp_path: Path) -> None:
    saved = _capture(tmp_path, create_tiny_run())
    repo_root = tmp_path / "repo"
    (repo_root / "data").mkdir(parents=True)
    (repo_root / "data" / ROWS_FILENAME).write_bytes(TINY_ROWS)
    _update_recipe_inputs(saved, {"at": RepoPathLocation(path=f"data/{ROWS_FILENAME}")})
    configure_throwaway_workspace(tmp_path / "ws")
    rebuilt = rebuild_run(saved, repo_root=repo_root, inputs_dir=None)
    assert read_run_manifest(rebuilt.project_id, rebuilt.run_id).status == RunStatus.OK


def test_rebuild_binds_every_input_of_a_stage_in_recipe_order(tmp_path: Path) -> None:
    saved = _capture(tmp_path, create_tiny_run())
    header, *rows = TINY_ROWS.splitlines(keepends=True)
    # Recipe order is not name order, so binding the files sorted by name fails too.
    files = {"top_rows.csv": b"".join([header, *rows[:-1]]), "bottom_rows.csv": header + rows[-1]}
    inputs = [_record_supplied_input(filename, content) for filename, content in files.items()]
    write_recipe(saved, read_recipe(saved).model_copy(update={"inputs": inputs}))
    rebuilt = _rebuild(tmp_path, saved, _write_inputs(tmp_path, files))
    manifest = read_run_manifest(rebuilt.project_id, rebuilt.run_id)
    assert [binding.filename for binding in read_input_bindings(manifest.to_dict())] == list(files)


def test_rebuild_refuses_every_bad_input_at_once_before_importing_anything(tmp_path: Path) -> None:
    saved = _capture(tmp_path, create_tiny_run())
    recipe = read_recipe(saved)
    second = _record_supplied_input("more_rows.csv", TINY_ROWS)
    write_recipe(saved, recipe.model_copy(update={"inputs": [*recipe.inputs, second]}))
    inputs_dir = _write_inputs(tmp_path, {second.filename: _MORE_ROWS})
    reasons = _collect_refusal_reasons(tmp_path, saved, inputs_dir)
    assert any(str(inputs_dir / ROWS_FILENAME) in reason for reason in reasons), reasons
    assert any(f"'{second.filename}'" in reason for reason in reasons), reasons
    assert project.list_projects() == []


def test_rebuild_refuses_an_archive_whose_cache_no_longer_fits_its_workflow(tmp_path: Path) -> None:
    run = create_reviewed_run_past_a_queue_that_caches()
    saved = _capture(tmp_path, run)
    _save_a_version_that_instructs_reviewers(run.project_id)
    _replace_archive(saved, export_project_archive(run.project_id))
    inputs_dir = _write_inputs(tmp_path, {ROWS_FILENAME: TINY_ROWS})
    [reason] = _collect_refusal_reasons(tmp_path, saved, inputs_dir)
    assert "no longer fits the workflow" in reason and "capture the run again" in reason


def test_rebuild_refuses_a_different_end_status(tmp_path: Path) -> None:
    saved = _capture(tmp_path, create_tiny_run())
    write_recipe(saved, read_recipe(saved).model_copy(update={"ends": RunStatus.WARNINGS}))
    inputs_dir = _write_inputs(tmp_path, {ROWS_FILENAME: TINY_ROWS})
    [reason] = _collect_refusal_reasons(tmp_path, saved, inputs_dir)
    assert "'ok'" in reason and "'warnings'" in reason


def test_rebuild_refuses_a_figure_that_moved_printing_both_values(tmp_path: Path) -> None:
    saved = _capture(tmp_path, create_tiny_run())
    _rewrite_recipe_figure(saved, "amount-total", 13)
    inputs_dir = _write_inputs(tmp_path, {ROWS_FILENAME: TINY_ROWS})
    [reason] = _collect_refusal_reasons(tmp_path, saved, inputs_dir)
    assert "'amount-total'" in reason and "published 12;" in reason and "recorded 13" in reason


def test_rebuild_refuses_a_figure_whose_value_changed_type_printing_both_values(
    tmp_path: Path,
) -> None:
    saved = _capture(tmp_path, create_tiny_run())
    _rewrite_recipe_figure(saved, "amount-total", 12.0)
    inputs_dir = _write_inputs(tmp_path, {ROWS_FILENAME: TINY_ROWS})
    [reason] = _collect_refusal_reasons(tmp_path, saved, inputs_dir)
    assert "'amount-total'" in reason and "published 12;" in reason and "recorded 12.0" in reason


def test_rebuild_refuses_a_figure_the_rebuilt_run_did_not_publish(tmp_path: Path) -> None:
    saved = _capture(tmp_path, create_tiny_run())
    recipe = read_recipe(saved)
    unpublished = RecipeFigure(slug="median-amount", stage_id=TOTALS_STAGE, value=4, claimable=False)
    write_recipe(saved, recipe.model_copy(update={"figures": [*recipe.figures, unpublished]}))
    inputs_dir = _write_inputs(tmp_path, {ROWS_FILENAME: TINY_ROWS})
    [reason] = _collect_refusal_reasons(tmp_path, saved, inputs_dir)
    assert "'median-amount'" in reason and "did not publish" in reason


def test_rebuild_refuses_a_figure_the_recipe_does_not_list(tmp_path: Path) -> None:
    saved = _capture(tmp_path, create_tiny_run())
    _drop_recipe_figure(saved, "largest-amount")
    inputs_dir = _write_inputs(tmp_path, {ROWS_FILENAME: TINY_ROWS})
    [reason] = _collect_refusal_reasons(tmp_path, saved, inputs_dir)
    assert "'largest-amount'" in reason and "does not list" in reason


def test_rebuild_refuses_a_rebuilt_run_that_publishes_one_slug_twice(tmp_path: Path) -> None:
    saved = _capture(tmp_path, create_tiny_run())
    # Neither repeated figure holds 13, so comparing either one would add a second reason.
    recorded = RecipeFigure(slug="amount-total", stage_id=TOTALS_STAGE, value=13, claimable=False)
    write_recipe(saved, read_recipe(saved).model_copy(update={"figures": [recorded]}))
    repeated = create_tiny_run_publishing_a_slug_twice()
    _replace_archive(saved, export_project_archive(repeated.project_id))
    inputs_dir = _write_inputs(tmp_path, {ROWS_FILENAME: TINY_ROWS})
    [reason] = _collect_refusal_reasons(tmp_path, saved, inputs_dir)
    assert "'amount-total'" in reason and "more than one" in reason


def test_a_second_rebuild_in_one_workspace_compares_only_its_own_figures(tmp_path: Path) -> None:
    saved = _capture(tmp_path, create_tiny_run())
    inputs_dir = _write_inputs(tmp_path, {ROWS_FILENAME: TINY_ROWS})
    first = _rebuild(tmp_path, saved, inputs_dir)
    second = rebuild_run(saved, repo_root=_REPO_ROOT, inputs_dir=inputs_dir)
    assert second.run_id != first.run_id


def test_rebuild_lists_every_failure_in_one_refusal(tmp_path: Path) -> None:
    saved = _capture(tmp_path, create_tiny_run())
    write_recipe(saved, read_recipe(saved).model_copy(update={"ends": RunStatus.WARNINGS}))
    _drop_recipe_figure(saved, "largest-amount")
    inputs_dir = _write_inputs(tmp_path, {ROWS_FILENAME: TINY_ROWS})
    reasons = _collect_refusal_reasons(tmp_path, saved, inputs_dir)
    assert len(reasons) == 2, reasons
    assert any("'warnings'" in reason for reason in reasons), reasons
    assert any("'largest-amount'" in reason for reason in reasons), reasons


def test_a_refusal_after_the_run_carries_and_names_the_rebuilt_project_and_run(
    tmp_path: Path,
) -> None:
    saved = _capture(tmp_path, create_tiny_run())
    write_recipe(saved, read_recipe(saved).model_copy(update={"ends": RunStatus.WARNINGS}))
    inputs_dir = _write_inputs(tmp_path, {ROWS_FILENAME: TINY_ROWS})
    configure_throwaway_workspace(tmp_path / "ws")
    with pytest.raises(RebuiltRunRefused) as refused:
        rebuild_run(saved, repo_root=_REPO_ROOT, inputs_dir=inputs_dir)
    [entry] = list_every_run_entry()
    assert refused.value.rebuilt == RebuiltRun(project_id=entry.project, run_id=entry.run_id)
    assert entry.project in str(refused.value) and entry.run_id in str(refused.value)


def test_rebuild_refuses_a_run_whose_model_stage_called_a_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = _capture(tmp_path, create_run_with_a_model_stage_that_does_not_cache())
    monkeypatch.setattr("app.runtime.stages.llm_transform.call_llm", _answer_recording_a_model_call)
    inputs_dir = _write_inputs(tmp_path, {ROWS_FILENAME: TINY_ROWS})
    [reason] = _collect_refusal_reasons(tmp_path, saved, inputs_dir)
    assert f"'{MODEL_STAGE}'" in reason and "called a model" in reason


def test_find_model_spend_names_each_stage_that_called_a_model() -> None:
    manifest = _build_manifest([
        _build_stage_record("load", StageType.input_data, None),
        _build_stage_record("classify", StageType.llm_transform, LlmUsage(calls=3)),
        _build_stage_record("replayed", StageType.llm_transform, None),
        _build_stage_record("switched_off", StageType.llm_transform, LlmUsage(calls=0)),
        _build_stage_record("score", StageType.python_row_function, LlmUsage(calls=2)),
        _build_stage_record("summarize", StageType.llm_transform, LlmUsage(calls=1)),
    ])
    assert find_model_spend(manifest) == ["classify", "score", "summarize"]


def test_a_rebuild_never_reaches_a_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    saved = _capture(tmp_path, create_run_with_a_model_stage_that_does_not_cache())
    inputs_dir = _write_inputs(tmp_path, {ROWS_FILENAME: TINY_ROWS})
    configure_throwaway_workspace(tmp_path / "ws")
    monkeypatch.setattr("app.runtime.options.agent_available", lambda: True)
    seen_at_the_model_gate: list[bool] = []
    model_prompts: list[str] = []
    monkeypatch.setattr(
        "app.runtime.llm.require_agent_backend", _record_availability(seen_at_the_model_gate)
    )
    monkeypatch.setattr("app.core.agent.sdk_engine.query", _record_model_calls(model_prompts))
    with pytest.raises(RunRefused) as refused:
        rebuild_run(saved, repo_root=_REPO_ROOT, inputs_dir=inputs_dir)
    assert seen_at_the_model_gate == [False] * _TINY_ROW_COUNT
    assert model_prompts == []
    [reason] = refused.value.reasons
    assert "ended 'errors'" in reason and "recorded 'ok'" in reason
    assert f"stage '{MODEL_STAGE}' failed with RowGenerationError" in reason
    assert "No LLM backend available" in reason


@pytest.mark.parametrize(
    ("limits", "outcome"),
    [({}, nullcontext()), ({"no_such_stage": 1}, pytest.raises(ValueError, match="unknown stage"))],
    ids=["returns", "raises"],
)
def test_rebuild_puts_agent_available_back_whether_it_returns_or_raises(
    tmp_path: Path, limits: dict[str, int], outcome: AbstractContextManager[object]
) -> None:
    saved = _capture(tmp_path, create_tiny_run())
    write_recipe(saved, read_recipe(saved).model_copy(update={"limits": limits}))
    available = options.agent_available
    inputs_dir = _write_inputs(tmp_path, {ROWS_FILENAME: TINY_ROWS})
    with outcome:
        _rebuild(tmp_path, saved, inputs_dir)
    assert options.agent_available is available


def _capture(tmp_path: Path, run: TinyRun) -> Path:
    return capture_run(
        run.project_id, run.run_id, tmp_path / "saved", repo_root=_REPO_ROOT, replace=False
    )


def _write_inputs(tmp_path: Path, files: dict[str, bytes]) -> Path:
    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir()
    for filename, content in files.items():
        (inputs_dir / filename).write_bytes(content)
    return inputs_dir


def _rebuild(tmp_path: Path, saved: Path, inputs_dir: Path | None) -> RebuiltRun:
    configure_throwaway_workspace(tmp_path / "ws")
    return rebuild_run(saved, repo_root=_REPO_ROOT, inputs_dir=inputs_dir)


def _collect_refusal_reasons(tmp_path: Path, saved: Path, inputs_dir: Path | None) -> list[str]:
    with pytest.raises(RunRefused) as refused:
        _rebuild(tmp_path, saved, inputs_dir)
    return refused.value.reasons


def _cite_totals_cell(run_id: str, column: str, value: int) -> StageOutputCellCitation:
    return StageOutputCellCitation(
        run_id=run_id, stage_id=TOTALS_STAGE, row_ordinal=0, column=column, value=value
    )


def _record_supplied_input(filename: str, content: bytes) -> RecipeInput:
    return RecipeInput(
        stage_id=LOAD_STAGE,
        filename=filename,
        bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        at=SuppliedLocation(),
    )


def _update_recipe_inputs(saved: Path, update: dict[str, object]) -> None:
    recipe = read_recipe(saved)
    updated = [recorded.model_copy(update=update) for recorded in recipe.inputs]
    write_recipe(saved, recipe.model_copy(update={"inputs": updated}))


def _replace_archive(saved: Path, archive: bytes) -> None:
    (saved / ARCHIVE_FILE).write_bytes(archive)
    digest = hashlib.sha256(archive).hexdigest()
    write_recipe(saved, read_recipe(saved).model_copy(update={"archive_sha256": digest}))


def _rewrite_recipe_figure(saved: Path, slug: str, value: JsonScalar) -> None:
    recipe = read_recipe(saved)
    figures = [
        figure.model_copy(update={"value": value}) if figure.slug == slug else figure
        for figure in recipe.figures
    ]
    write_recipe(saved, recipe.model_copy(update={"figures": figures}))


def _drop_recipe_figure(saved: Path, slug: str) -> None:
    recipe = read_recipe(saved)
    figures = [figure for figure in recipe.figures if figure.slug != slug]
    write_recipe(saved, recipe.model_copy(update={"figures": figures}))


def _save_a_version_that_instructs_reviewers(project_id: str) -> None:
    version = find_latest_version_id(project_id)
    assert version is not None, f"project {project_id} has no saved version"
    stages: list[Stage] = [
        _instruct_reviewers(stage) if isinstance(stage, HumanReviewQueueStage) else stage
        for stage in load_version_stages(project_id, version)
    ]
    save_stages(project_id, stages)
    project.save_working_copy_as_version(project_id, message="Instruct the reviewers")


def _instruct_reviewers(stage: HumanReviewQueueStage) -> HumanReviewQueueStage:
    queue = stage.queue.model_copy(update={"reviewer_instructions": "Check each amount twice."})
    return stage.model_copy(update={"queue": queue})


def _answer_recording_a_model_call(
    *_args: object, usage_out: list[LlmUsage], **_kwargs: object
) -> dict[str, str]:
    usage_out.append(LlmUsage(input_tokens=12, output_tokens=3, calls=1))
    return {"industry": "manufacturing"}


def _record_availability(seen: list[bool]) -> Callable[[], None]:
    def record_then_require_agent_backend() -> None:
        seen.append(options.agent_available())
        require_agent_backend()

    return record_then_require_agent_backend


def _record_model_calls(prompts: list[str]) -> Callable[..., AsyncIterator[object]]:
    async def query(*, prompt: str, **_options: object) -> AsyncIterator[object]:
        prompts.append(prompt)
        raise LLMError("a rebuild reached the model")
        yield  # unreachable: it makes this the async generator the engine iterates

    return query


def _build_manifest(records: list[StageRecord]) -> RunManifest:
    return RunManifest(
        run_id="20260915T120000.000000",
        started_at="2026-09-15T12:00:00",
        project="tiny",
        workflow_version="version-1",
        human_review_queue_stats={},
        status=RunStatus.OK,
        stage_records=records,
    )


def _build_stage_record(
    stage_id: str, stage_type: StageType, llm_usage: LlmUsage | None
) -> StageRecord:
    return StageRecord(
        stage_id=stage_id,
        type=stage_type,
        status=StageStatus.OK,
        input_validation_report=[],
        output_validation_report=None,
        output_row_count=3,
        llm_usage=llm_usage,
    )

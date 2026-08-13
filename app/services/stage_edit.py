"""The single validated writer for one compiled stage.

Every change is validated against the whole resulting workflow before anything is
written. Removal is the one direct disk touch (the stage's file is unlinked here);
every other write goes through the loader's `write_stage`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from typing import Sequence

from app.models import StageDraft, parse_stage, stage_to_spec_dict
from app.models.stages.code import SUMMARY_MAX_CHARS
from app.models.workflow import (
    detect_cycle,
    sort_stages_by_dependency,
    validate_unique_ids,
    validate_workflow_draft,
)
from app.services.loader import (
    LOADER_BOOKKEEPING_KEYS,
    find_stage_file,
    list_stage_files,
    load_workflow_object,
    write_stage,
)


@dataclass
class EditStageResult:
    ok: bool
    issues: list[str] = field(default_factory=list)


@dataclass
class StageFailure:
    id: str
    issues: list[str]


@dataclass
class SkippedStage:
    id: str
    because: str


@dataclass
class AddStagesResult:
    """`batch_issues` refuses the batch whole — the other three lists are then empty."""
    added: list[str] = field(default_factory=list)
    failed: list[StageFailure] = field(default_factory=list)
    skipped: list[SkippedStage] = field(default_factory=list)
    batch_issues: list[str] = field(default_factory=list)


def _merge_patch(target: object, patch: object) -> object:
    """RFC 7386 JSON Merge Patch."""
    if not isinstance(patch, dict):
        return patch
    base: dict[str, object] = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            base.pop(key, None)
        else:
            base[key] = _merge_patch(base.get(key), value)
    return base


def _current_specs(project_dir: Path) -> dict[str, dict]:
    """An EMPTY workflow reads as {}; a load failure raises — never read a failure as emptiness."""
    if not list_stage_files(project_dir / "compiled"):
        return {}
    workflow = load_workflow_object(project_dir)
    return {stage.id: stage_to_spec_dict(stage) for stage in workflow.stages}


# The config blocks whose behaviour is authored code, so a reviewer cannot read the
# stage without prose standing in for it — the two that carry `summary`.
_AUTHORED_CODE_BLOCKS = ("function", "filter")


def find_description_issues(candidate: dict) -> list[str]:
    """Enforced on write, not on the model — on load it would refuse every stage stored before."""
    for block_name in _AUTHORED_CODE_BLOCKS:
        block = candidate.get(block_name)
        if not isinstance(block, dict):
            continue
        issues = []
        summary = (block.get("summary") or "").strip()
        if len(summary) > SUMMARY_MAX_CHARS:
            issues.append(
                f"`{block_name}.summary` is {len(summary)} characters; the limit is "
                f"{SUMMARY_MAX_CHARS}. A summary a non-engineer will actually read is "
                f"short — state the rule and stop. Anything conditional or surprising "
                f"belongs in `corner_cases`, which has no limit."
            )
        if not summary:
            issues.append(
                f"`{block_name}.summary` is required: this stage's behaviour is authored "
                f"code, and the person reviewing it reads prose, not Python. Write one "
                f"or two plain sentences saying what the step does — the rule, not the "
                f"implementation — in the same edit as the code."
            )
        if "corner_cases" not in block:
            issues.append(
                f"`{block_name}.corner_cases` must be submitted: one entry per input whose "
                f"handling the summary does not state, each with the outcome it must "
                f"produce. Send `[]` if this step genuinely has none — that is a valid "
                f"answer, but it has to be said rather than left out."
            )
        return issues
    return []


def _strip_bookkeeping_keys(spec: dict) -> dict:
    return {k: v for k, v in spec.items() if k not in LOADER_BOOKKEEPING_KEYS}


def _apply(project_dir: Path, specs: dict[str, dict], stage_id: str, candidate: dict) -> EditStageResult:
    candidate = _strip_bookkeeping_keys(candidate)
    if candidate.get("id") != stage_id:
        return EditStageResult(
            ok=False,
            issues=[f"the stage id must equal '{stage_id}' (got '{candidate.get('id')}')"],
        )

    resulting = {**specs, stage_id: candidate}
    issues = validate_workflow_draft(list(resulting.values()))
    issues += find_description_issues(candidate)
    if issues:
        return EditStageResult(ok=False, issues=issues)

    validated_stage = parse_stage(candidate)
    # Overwrite the stage's existing file if it has one; a new stage is named by
    # its id (file order is irrelevant — the workflow order is the input_ids DAG).
    compiled_dir = project_dir / "compiled"
    target = find_stage_file(compiled_dir, stage_id) or compiled_dir / f"{stage_id}.json"
    compiled_dir.mkdir(parents=True, exist_ok=True)  # the first stage creates it
    write_stage(target, validated_stage)
    return EditStageResult(ok=True)


def edit_stage_spec(project_dir: Path, stage_id: str, spec_text: str) -> EditStageResult:
    try:
        spec = json.loads(spec_text)
    except json.JSONDecodeError as exc:
        return EditStageResult(ok=False, issues=[f"JSON parse error: {exc}"])
    if not isinstance(spec, dict):
        return EditStageResult(ok=False, issues=["edited spec must be a JSON object (a single stage)"])
    specs = _current_specs(project_dir)
    if stage_id not in specs:
        raise FileNotFoundError(f"no stage '{stage_id}' in {project_dir.name}")
    return _apply(project_dir, specs, stage_id, spec)


def patch_stage_spec(project_dir: Path, stage_id: str, patch_text: str) -> EditStageResult:
    try:
        patch = json.loads(patch_text)
    except json.JSONDecodeError as exc:
        return EditStageResult(ok=False, issues=[f"JSON parse error: {exc}"])
    if not isinstance(patch, dict):
        return EditStageResult(ok=False, issues=["changes must be a JSON object of {field: new_value}"])
    specs = _current_specs(project_dir)
    if stage_id not in specs:
        raise FileNotFoundError(f"no stage '{stage_id}' in {project_dir.name}")
    merged = _merge_patch(specs[stage_id], patch)
    assert isinstance(merged, dict)  # both inputs are dicts, so the merge is too
    return _apply(project_dir, specs, stage_id, merged)


def add_stage_specs(project_dir: Path, stages: Sequence[StageDraft]) -> AddStagesResult:
    batch_issues = validate_unique_ids(stages) + detect_cycle(stages)
    if batch_issues:
        return AddStagesResult(batch_issues=batch_issues)

    result = AddStagesResult()
    specs = _current_specs(project_dir)
    for stage in sort_stages_by_dependency(stages):
        blocker = _find_blocking_input(stage, result)
        if blocker is not None:
            result.skipped.append(SkippedStage(stage.id, f"inputs from {blocker}"))
            continue
        spec = stage.to_stage_spec()
        outcome = _add_new_stage(project_dir, specs, spec)
        if not outcome.ok:
            result.failed.append(StageFailure(stage.id, outcome.issues))
            continue
        specs[stage.id] = _strip_bookkeeping_keys(spec)
        result.added.append(stage.id)
    return result


def _find_blocking_input(stage: StageDraft, result: AddStagesResult) -> str | None:
    unavailable = {f.id for f in result.failed} | {s.id for s in result.skipped}
    return next((i for i in stage.input_ids if i in unavailable), None)


def add_stage_spec(project_dir: Path, spec_text: str) -> EditStageResult:
    try:
        spec = json.loads(spec_text)
    except json.JSONDecodeError as exc:
        return EditStageResult(ok=False, issues=[f"JSON parse error: {exc}"])
    if not isinstance(spec, dict):
        return EditStageResult(ok=False, issues=["new stage must be a JSON object (a single stage)"])
    return _add_new_stage(project_dir, _current_specs(project_dir), spec)


def _add_new_stage(project_dir: Path, specs: dict[str, dict], spec: dict) -> EditStageResult:
    stage_id = spec.get("id")
    if not isinstance(stage_id, str) or not stage_id:
        return EditStageResult(ok=False, issues=["new stage must have a non-empty string 'id'"])
    if stage_id in specs:
        return EditStageResult(
            ok=False,
            issues=[f"stage '{stage_id}' already exists — use edit_stage to change it"],
        )
    return _apply(project_dir, specs, stage_id, spec)


def remove_stage_spec(project_dir: Path, stage_id: str) -> EditStageResult:
    specs = _current_specs(project_dir)
    if stage_id not in specs:
        raise FileNotFoundError(f"no stage '{stage_id}' in {project_dir.name}")

    resulting = {k: v for k, v in specs.items() if k != stage_id}
    issues = validate_workflow_draft(list(resulting.values()))
    if issues:
        return EditStageResult(ok=False, issues=issues)

    target = find_stage_file(project_dir / "compiled", stage_id)
    if target is not None:
        target.unlink()
    return EditStageResult(ok=True)

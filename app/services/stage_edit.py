"""The single validated writer for one stage of a project's working copy.

Every change is validated against the whole resulting workflow before anything is
written; every write goes through the loader's `save_stage_specs`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from typing import Callable, Sequence

from app.core.json_types import JsonDict

from app.core.llm import LLMModel
from app.models.stages.aggregate import RETIRED_FORMULAS
from app.models.stages.stage_types import APPROVAL_REQUIRED_TYPES
from app.services.code_approval import has_code_execution_approval
from app.models import StageDraft, StageEdit
from app.models.stages.code import SUMMARY_MAX_CHARS
from app.models.workflow import (
    detect_cycle,
    sort_stages_by_dependency,
    validate_unique_ids,
    validate_workflow_draft,
)
from app.services.loader import (
    exists as has_working_copy,
    read_stage_specs,
    save_stage_specs,
    index_stage_specs_by_id,
)


@dataclass(frozen=True)
class StageSpecStore:
    """Where an edit reads its stages and writes them back: a draft, or the working copy."""

    project_id: str
    read: Callable[[], dict[str, JsonDict]]
    write: Callable[[list[JsonDict]], None]


def open_working_copy(project_id: str) -> StageSpecStore:
    return StageSpecStore(
        project_id=project_id,
        read=lambda: _working_copy_specs(project_id),
        write=lambda specs: save_stage_specs(project_id, specs),
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


def _working_copy_specs(project_id: str) -> dict[str, JsonDict]:
    """An EMPTY workflow reads as {}; a load failure raises — never read a failure as emptiness."""
    if not has_working_copy(project_id) or not read_stage_specs(project_id):
        return {}
    return index_stage_specs_by_id(project_id)


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


CODE_EXECUTION_REFUSAL = (
    "stage '{sid}': `{stage_type}` runs Python this project has not approved. Carbon "
    "Paper is not built for arbitrary code execution — a Python step runs on the "
    "machine hosting this project, with its permissions: it can read files, reach the "
    "network and install packages, and nothing here inspects what it does. It also "
    "reshapes the table opaquely, so a trace stops at it and a published figure cannot "
    "be walked back to the rows behind it.\n"
    "Most of what this type is used for no longer needs it: `explode` unpacks a list "
    "column into rows and a `starlark_row_function` after it can do the per-row work "
    "sandboxed; `dedupe`, `sort_rank`, `aggregate`, `enrich`, `expand` and `union` cover "
    "the rest of the reshapes. Try those first.\n"
    "If this genuinely needs Python, tell the project's owner what it will do and why no "
    "declared stage fits, and ask whether to turn code execution on for this project. "
    "Only once THEY have answered yes, call `approve_code_execution`."
)


def find_unapproved_code_issues(
    project_id: str, candidate: dict, stored: dict[str, dict] | None = None
) -> list[str]:
    """Gates INTRODUCING one of these types, never maintaining a stage that already is one."""
    stage_type = candidate.get("type")
    if stage_type not in APPROVAL_REQUIRED_TYPES:
        return []
    # A stored stage of this type keeps loading and running, so it must stay
    # editable: generating its tests, fixing its summary, correcting its code.
    # Refusing that would strand every project holding one — and the approval it
    # would ask for was already given, or predates the gate.
    if (stored or {}).get(candidate.get("id", ""), {}).get("type") == stage_type:
        return []
    if has_code_execution_approval(project_id):
        return []
    return [CODE_EXECUTION_REFUSAL.format(sid=candidate.get("id"), stage_type=stage_type)]


AGGREGATION_WHERE_REFUSAL = (
    "stage '{sid}': aggregation `{output_column}` carries `where`, which is retired — a "
    "row-cut no stage shows, leaving that row's columns each resting on different rows. "
    "Group on the column `{predicate}` tests, or put a filter_rows stage in front."
)

AGGREGATION_PICK_REFUSAL = (
    "stage '{sid}': aggregation `{output_column}` uses `{formula}`, which is retired — it "
    "picks between rows that may disagree and drops the rest unrecorded. Use `only` where "
    "the group agrees, `list` where it does not, or put a dedupe stage in front."
)


def find_aggregation_issues(candidate: dict) -> list[str]:
    """Enforced on write, not on the model — on load it would refuse every version stored before."""
    aggregate = candidate.get("aggregate")
    aggregations = aggregate.get("aggregations") if isinstance(aggregate, dict) else None
    if not isinstance(aggregations, list):
        return []
    issues: list[str] = []
    for op in aggregations:
        if not isinstance(op, dict):
            continue
        if op.get("where"):
            issues.append(AGGREGATION_WHERE_REFUSAL.format(
                sid=candidate.get("id"), output_column=op.get("output_column"),
                predicate=op.get("where")))
        if op.get("formula") in RETIRED_FORMULAS:
            issues.append(AGGREGATION_PICK_REFUSAL.format(
                sid=candidate.get("id"), output_column=op.get("output_column"),
                formula=op.get("formula")))
    return issues


def find_unnamed_model_issues(candidate: dict) -> list[str]:
    """Enforced on write, not on the model — on load it would refuse every llm stage stored before."""
    llm = candidate.get("llm")
    if not isinstance(llm, dict) or llm.get("model"):
        return []
    return [
        "`llm.model` is required: a run records which model answered its rows, and a "
        "stage that names none is answered by whatever the deployment defaults to on "
        f"the day it runs. Name one of {[member.value for member in LLMModel]}."
    ]


def _apply(store: StageSpecStore, specs: dict[str, JsonDict], candidates: dict[str, JsonDict]) -> EditStageResult:
    misnamed = [
        f"the stage id must equal '{stage_id}' (got '{candidate.get('id')}')"
        for stage_id, candidate in candidates.items()
        if candidate.get("id") != stage_id
    ]
    if misnamed:
        return EditStageResult(ok=False, issues=misnamed)

    resulting = {**specs, **candidates}
    issues = validate_workflow_draft(list(resulting.values()))
    for candidate in candidates.values():
        issues += find_description_issues(candidate)
        issues += find_unnamed_model_issues(candidate)
        issues += find_unapproved_code_issues(store.project_id, candidate, specs)
        issues += find_aggregation_issues(candidate)
    if issues:
        return EditStageResult(ok=False, issues=issues)

    # An existing stage keeps its position; a new one lands at the end. Stored
    # order is presentation only — the workflow order is the input_ids DAG.
    store.write(list(resulting.values()))
    return EditStageResult(ok=True)


def edit_stage_spec(store: StageSpecStore, stage_id: str, spec_text: str) -> EditStageResult:
    try:
        spec = json.loads(spec_text)
    except json.JSONDecodeError as exc:
        return EditStageResult(ok=False, issues=[f"JSON parse error: {exc}"])
    if not isinstance(spec, dict):
        return EditStageResult(ok=False, issues=["edited spec must be a JSON object (a single stage)"])
    specs = store.read()
    if stage_id not in specs:
        raise FileNotFoundError(f"no stage '{stage_id}' in project '{store.project_id}'")
    return _apply(store, specs, {stage_id: spec})


def patch_stage_specs(store: StageSpecStore, edits: Sequence[StageEdit]) -> EditStageResult:
    """Every edit lands or none does: the merged workflow is validated once, then written once."""
    specs = store.read()
    merged: dict[str, dict] = {}
    for edit in edits:
        if edit.stage_id not in specs:
            raise FileNotFoundError(f"no stage '{edit.stage_id}' in project '{store.project_id}'")
        try:
            patch = json.loads(edit.changes_json)
        except json.JSONDecodeError as exc:
            return EditStageResult(ok=False, issues=[f"`{edit.stage_id}`: JSON parse error: {exc}"])
        if not isinstance(patch, dict):
            return EditStageResult(ok=False, issues=[
                f"`{edit.stage_id}`: changes must be a JSON object of {{field: new_value}}"])
        # Merging onto what an earlier edit produced, so two patches to one stage compose.
        patched = _merge_patch(merged.get(edit.stage_id, specs[edit.stage_id]), patch)
        assert isinstance(patched, dict)  # both inputs are dicts, so the merge is too
        merged[edit.stage_id] = patched
    return _apply(store, specs, merged)


def add_stage_specs(store: StageSpecStore, stages: Sequence[StageDraft]) -> AddStagesResult:
    batch_issues = validate_unique_ids(stages) + detect_cycle(stages)
    if batch_issues:
        return AddStagesResult(batch_issues=batch_issues)

    result = AddStagesResult()
    specs = store.read()
    for stage in sort_stages_by_dependency(stages):
        blocker = _find_blocking_input(stage, result)
        if blocker is not None:
            result.skipped.append(SkippedStage(stage.id, f"inputs from {blocker}"))
            continue
        spec = stage.to_stage_spec()
        outcome = _add_new_stage(store, specs, spec)
        if not outcome.ok:
            result.failed.append(StageFailure(stage.id, outcome.issues))
            continue
        specs[stage.id] = spec
        result.added.append(stage.id)
    return result


def _find_blocking_input(stage: StageDraft, result: AddStagesResult) -> str | None:
    unavailable = {f.id for f in result.failed} | {s.id for s in result.skipped}
    return next((i for i in stage.input_ids if i in unavailable), None)


def add_stage_spec(store: StageSpecStore, spec_text: str) -> EditStageResult:
    try:
        spec = json.loads(spec_text)
    except json.JSONDecodeError as exc:
        return EditStageResult(ok=False, issues=[f"JSON parse error: {exc}"])
    if not isinstance(spec, dict):
        return EditStageResult(ok=False, issues=["new stage must be a JSON object (a single stage)"])
    return _add_new_stage(store, store.read(), spec)


def _add_new_stage(store: StageSpecStore, specs: dict[str, JsonDict], spec: JsonDict) -> EditStageResult:
    stage_id = spec.get("id")
    if not isinstance(stage_id, str) or not stage_id:
        return EditStageResult(ok=False, issues=["new stage must have a non-empty string 'id'"])
    if stage_id in specs:
        return EditStageResult(
            ok=False,
            issues=[f"stage '{stage_id}' already exists — use edit_stages to change it"],
        )
    return _apply(store, specs, {stage_id: spec})


def delete_stage_spec(store: StageSpecStore, stage_id: str) -> EditStageResult:
    specs = store.read()
    if stage_id not in specs:
        raise FileNotFoundError(f"no stage '{stage_id}' in project '{store.project_id}'")

    resulting = {k: v for k, v in specs.items() if k != stage_id}
    issues = validate_workflow_draft(list(resulting.values()))
    if issues:
        return EditStageResult(ok=False, issues=issues)

    store.write(list(resulting.values()))
    return EditStageResult(ok=True)

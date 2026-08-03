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

from app.models import StageDraft, parse_stage
from app.models.stages.code import SUMMARY_MAX_CHARS
from app.models.workflow import (
    detect_cycle,
    sort_stages_by_dependency,
    validate_unique_ids,
    validate_workflow_draft,
)
from app.services import node_review
from app.services.loader import (
    find_stage_file,
    list_stage_files,
    load_workflow_object,
    stage_to_spec_dict,
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
    """What became of each stage in one `add_stage_specs` batch. `batch_issues`
    is a refusal of the batch as a whole, before any stage was attempted — the
    other three lists are then empty."""
    added: list[str] = field(default_factory=list)
    failed: list[StageFailure] = field(default_factory=list)
    skipped: list[SkippedStage] = field(default_factory=list)
    batch_issues: list[str] = field(default_factory=list)


def _merge_patch(target: object, patch: object) -> object:
    """RFC 7386 JSON Merge Patch: deep-merge objects, replace scalars/arrays, and
    delete a key when its patch value is null. A patch therefore touches only the
    fields it names — everything else is preserved verbatim."""
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
    """The workflow's current stages as ``{id: spec dict}``.

    A workflow may legitimately be EMPTY — a project holds no stage files until its
    first stage is added — and that reads as ``{}``, the starting point the first
    ``add_stage_spec`` builds on (and against which every stage id is unknown, so
    edit/patch/remove raise FileNotFoundError).

    A workflow that HAS stage files must load cleanly: it is read through the strict
    loader as one in-memory ``Workflow``, so anything unparseable or invalid raises
    rather than letting an edit proceed against a partial view of it. The two cases
    are told apart by whether ``list_stage_files`` finds any files at all — never by
    interpreting a load failure as emptiness."""
    if not list_stage_files(project_dir / "compiled"):
        return {}
    workflow = load_workflow_object(project_dir)
    return {stage.id: stage_to_spec_dict(stage) for stage in workflow.stages}


# The config blocks whose behaviour is authored code, so a reviewer cannot read the
# stage without prose standing in for it — the two that carry `summary`.
_AUTHORED_CODE_BLOCKS = ("function", "filter")


def _find_description_issues(candidate: dict) -> list[str]:
    """Refuse to WRITE a code-carrying stage whose description is not fully
    submitted ([] otherwise): `summary` must be non-blank, and `corner_cases` must
    be PRESENT — an empty list is a valid answer, an absent key is not.

    The asymmetry is the point. A step may genuinely have no awkward inputs, so
    requiring a non-empty list would make an agent pad it and invent behaviour. But
    letting the key be omitted makes "none" and "I did not consider it"
    indistinguishable, and those are the two states a reviewer most needs told
    apart. `corner_cases: []` is an author saying so on the record.

    Enforced here rather than on the model, and rather than inside
    `validate_workflow_draft`: the model keeps `summary` optional and the draft
    validator is shared with the loader, so requiring it in either place would
    refuse every stage stored before the field existed, and every frozen version,
    at load time. This is the authoring boundary — every write, from the node
    editor, the MCP tools and the compiler agent alike, funnels through `_apply` —
    so a stage can only ARRIVE without a description, never be created without one.

    A prose instruction in the type's contract notes asks for both; this is what
    makes it true."""
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
    """A submitted spec reduced to the keys the workflow stores — the form that
    goes into the in-memory `specs` map and onto disk."""
    return {k: v for k, v in spec.items() if k not in node_review.HASH_IGNORED_KEYS}


def _apply(project_dir: Path, specs: dict[str, dict], stage_id: str, candidate: dict) -> EditStageResult:
    """Apply ``candidate`` as stage ``stage_id`` to the in-memory workflow ``specs``,
    validate the whole resulting workflow (per-stage AND graph, via the same
    `validate_workflow_draft` the loader enforces), and only if clean persist the
    one stage through the loader. Returns issues and writes nothing otherwise.

    The writer reports only whether the write succeeded; it does not compute the
    node's review colour (content hash / approval state). A caller that needs the
    new colour re-derives it from the freshly-written stage."""
    candidate = _strip_bookkeeping_keys(candidate)
    if candidate.get("id") != stage_id:
        return EditStageResult(
            ok=False,
            issues=[f"the stage id must equal '{stage_id}' (got '{candidate.get('id')}')"],
        )

    resulting = {**specs, stage_id: candidate}
    issues = validate_workflow_draft(list(resulting.values()))
    issues += _find_description_issues(candidate)
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
    """Replace `stage_id`'s spec with `spec_text` (a whole stage as JSON) — used by
    the human node editor, which submits the full spec it is showing. Returns
    issues (and writes nothing) on any parse/validation problem. Raises
    FileNotFoundError if `stage_id` is not a stage in this workflow."""
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
    """Apply `patch_text` (a JSON Merge Patch, RFC 7386) to `stage_id`'s current
    spec: only the fields named in the patch change, everything else is preserved
    verbatim, and a null value deletes a field. Used by the editing agent so it
    cannot drift on fields it was not asked to touch. Returns issues (and writes
    nothing) on any parse/validation problem. Raises FileNotFoundError if the stage
    does not exist."""
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
    """Add several NEW stages in one pass, keeping everything that validates.

    The stages are ordered by their declared `inputs`, so a caller may submit them
    in any order, and each is validated against the whole workflow-so-far — the
    stages already stored plus the ones accepted earlier in this batch. A stage
    that fails is not written and does not stop the batch; the stages that depend
    on it, directly or through another skipped stage, are skipped rather than
    attempted, since they could only fail on the input that is now missing.

    A batch that cannot be ordered at all — duplicate ids, or a cycle among the
    submitted stages — is refused whole, with nothing written."""
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
    """The id of the first input this stage names that the batch has already
    failed or skipped — the NEAREST cause of skipping this one, not the root."""
    unavailable = {f.id for f in result.failed} | {s.id for s in result.skipped}
    return next((i for i in stage.input_ids if i in unavailable), None)


def add_stage_spec(project_dir: Path, spec_text: str) -> EditStageResult:
    """Create a NEW stage from `spec_text` (a whole stage as JSON). The id must not
    already exist (use edit for an existing one). The resulting whole workflow is
    validated — a dangling input (or any per-stage / graph problem) is rejected,
    not written. The new stage lands as a fresh unreviewed (amber) node."""
    try:
        spec = json.loads(spec_text)
    except json.JSONDecodeError as exc:
        return EditStageResult(ok=False, issues=[f"JSON parse error: {exc}"])
    if not isinstance(spec, dict):
        return EditStageResult(ok=False, issues=["new stage must be a JSON object (a single stage)"])
    return _add_new_stage(project_dir, _current_specs(project_dir), spec)


def _add_new_stage(project_dir: Path, specs: dict[str, dict], spec: dict) -> EditStageResult:
    """Validate one new stage against `specs` and, if clean, write it. Does not
    mutate `specs`: a caller adding several stages records the accepted spec."""
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
    """Delete stage `stage_id` from the workflow. The REDUCED workflow is validated
    first, so a removal another stage still inputs from is rejected (its dangling
    input fails the graph check) and nothing is unlinked. Removing the last stage
    is allowed. Raises FileNotFoundError if the stage does not exist."""
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

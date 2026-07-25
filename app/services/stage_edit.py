"""stage_edit.py — the single validated writer for one compiled stage.

Extracted from the node-edit route so the route and the editing agent's tools
share ONE writer. It never touches disk itself: it loads the current workflow
through the loader (`Stage` objects, not raw files), applies the change to the
in-memory stage set, validates the whole resulting workflow, and only if that is
clean persists the one stage through `write_stage` — creating the project's
compiled/ dir when the stage being written is its first. Every change is validated
against the whole resulting workflow before anything is written.

Removal is the one direct disk touch: there is no stage to write, so once the
reduced workflow validates clean the stage's file — located through the loader's
`find_stage_file` — is unlinked here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.models import Stage
from app.models.workflow import validate_workflow_draft
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
    """The workflow's current stages as ``{id: canonical spec dict}``.

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


def _apply(project_dir: Path, specs: dict[str, dict], stage_id: str, candidate: dict) -> EditStageResult:
    """Apply ``candidate`` as stage ``stage_id`` to the in-memory workflow ``specs``,
    validate the whole resulting workflow (per-stage AND graph, via the same
    `validate_workflow_draft` the loader enforces), and only if clean persist the
    one stage through the loader. Returns issues and writes nothing otherwise.

    The writer reports only whether the write succeeded; it does not compute the
    node's review colour (content hash / approval state). A caller that needs the
    new colour re-derives it from the freshly-written stage."""
    candidate = {k: v for k, v in candidate.items() if k not in node_review.CANONICAL_IGNORE_KEYS}
    if candidate.get("id") != stage_id:
        return EditStageResult(
            ok=False,
            issues=[f"the stage id must equal '{stage_id}' (got '{candidate.get('id')}')"],
        )

    resulting = {**specs, stage_id: candidate}
    issues = validate_workflow_draft(list(resulting.values()))
    if issues:
        return EditStageResult(ok=False, issues=issues)

    validated_stage = Stage.model_validate(candidate)
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
    stage_id = spec.get("id")
    if not isinstance(stage_id, str) or not stage_id:
        return EditStageResult(ok=False, issues=["new stage must have a non-empty string 'id'"])
    specs = _current_specs(project_dir)
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

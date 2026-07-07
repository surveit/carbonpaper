"""stage_edit.py — the single validated writer for one compiled stage.

Extracted from the node-edit route so the route and the editing agent's
`edit_stage` tool share ONE writer: same validation (`validate_stage`), same
canonical form + hash (so an edit recolours the DAG identically), same refusal to
write an invalid spec. Lives here (not in node_review.py, which is free of
app.models) because validating requires the Stage model. All on-disk I/O goes
through the loader."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.models import Stage, validate_stage
from app.services import node_review
from app.services.loader import find_stage_file, stage_to_spec_dict, write_stage


@dataclass
class EditStageResult:
    ok: bool
    issues: list[str] = field(default_factory=list)
    content_hash: str | None = None
    state: str | None = None


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


def _current_stage(project_dir: Path, stage_id: str) -> dict:
    """Read one stage's current on-disk spec as a dict. Raises FileNotFoundError
    if it does not exist — edit revises, it never creates."""
    target = find_stage_file(project_dir / "compiled", stage_id)
    if target is None:
        raise FileNotFoundError(f"no existing compiled file for stage '{stage_id}' in {project_dir.name}")
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"compiled file for stage '{stage_id}' is not a JSON object")
    return data


def _write_validated(project_dir: Path, stage_id: str, stage: dict) -> EditStageResult:
    """Shared tail for both writers: strip non-spec keys, enforce the id, validate,
    and only then overwrite the stage's compiled file. Returns issues and writes
    nothing on any problem; the returned hash/state recolour the DAG."""
    stage = {k: v for k, v in stage.items() if k not in node_review.CANONICAL_IGNORE_KEYS}

    parsed_id = stage.get("id")
    if parsed_id != stage_id:
        return EditStageResult(
            ok=False,
            issues=[f"the stage id must equal '{stage_id}' (got '{parsed_id}')"],
        )

    issues = validate_stage(stage)
    if issues:
        return EditStageResult(ok=False, issues=issues)

    target = find_stage_file(project_dir / "compiled", stage_id)
    if target is None:
        raise FileNotFoundError(f"no existing compiled file for stage '{stage_id}' in {project_dir.name}")

    validated = Stage.model_validate(stage)
    write_stage(target, validated)

    spec = stage_to_spec_dict(validated)
    content_hash = node_review.node_content_hash(spec)
    decisions = node_review.load_node_decisions(project_dir)
    state = node_review.approval_state_for(spec, decisions)["state"]
    return EditStageResult(ok=True, content_hash=content_hash, state=state)


def edit_stage_spec(project_dir: Path, stage_id: str, spec_text: str) -> EditStageResult:
    """Replace `stage_id`'s spec with `spec_text` (a whole stage as JSON) — used by
    the human node editor, which submits the full spec it is showing. Returns
    issues (and writes nothing) on any parse/validation problem. Raises
    FileNotFoundError if no compiled file for `stage_id` exists."""
    try:
        parsed = json.loads(spec_text)
    except json.JSONDecodeError as exc:
        return EditStageResult(ok=False, issues=[f"JSON parse error: {exc}"])
    if not isinstance(parsed, dict):
        return EditStageResult(ok=False, issues=["edited spec must be a JSON object (a single stage)"])
    return _write_validated(project_dir, stage_id, parsed)


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
    merged = _merge_patch(_current_stage(project_dir, stage_id), patch)
    assert isinstance(merged, dict)  # both inputs are dicts, so the merge is too
    return _write_validated(project_dir, stage_id, merged)

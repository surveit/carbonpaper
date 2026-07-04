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


def edit_stage_spec(project_dir: Path, stage_id: str, spec_text: str) -> EditStageResult:
    """Validate `spec_text` (a single stage as JSON) as the new spec for
    `stage_id` and, only if clean, overwrite that stage's existing compiled file.
    Returns issues (and writes nothing) on any parse/validation problem. Raises
    FileNotFoundError if no compiled file for `stage_id` exists — edit revises, it
    never creates."""
    try:
        parsed = json.loads(spec_text)
    except json.JSONDecodeError as exc:
        return EditStageResult(ok=False, issues=[f"JSON parse error: {exc}"])
    if not isinstance(parsed, dict):
        return EditStageResult(ok=False, issues=["edited spec must be a JSON object (a single stage)"])

    stage = {k: v for k, v in parsed.items() if k not in node_review.CANONICAL_IGNORE_KEYS}

    parsed_id = stage.get("id")
    if parsed_id != stage_id:
        return EditStageResult(
            ok=False,
            issues=[f"id in the edited spec ('{parsed_id}') must equal the stage id '{stage_id}'"],
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

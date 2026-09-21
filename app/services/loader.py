"""A project's stages: its newest version's, parsed one spec at a time. Nothing writes."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from pydantic import ValidationError

from app.core.timestamp_ids import read_orderable_stamp
from app.core.json_types import JsonDict
from app.models.stage import Stage, parse_stage, stage_to_spec_dict
from app.models.workflow import Workflow, validate_workflow
from app.core.utils import format_errors

from .errors import WorkflowLoadError
from .versioning import read_latest_version_raw


@dataclass
class StageEntry:
    """One stored stage: its parsed Stage (None if invalid) and any issues."""
    label: str
    stage: Stage | None = None
    issues: list[str] = field(default_factory=list)


def list_parsed_stages(entries: list[StageEntry]) -> list[Stage]:
    return [entry.stage for entry in entries if entry.stage is not None]


def find_file_issues(entries: list[StageEntry]) -> list[str]:
    return [f"{entry.label}: {issue}" for entry in entries for issue in entry.issues]


def find_parsed_stage(entries: list[StageEntry], stage_id: str) -> Stage | None:
    return next((s for s in list_parsed_stages(entries) if s.id == stage_id), None)


def has_stages(project_id: str) -> bool:
    """Whether any version is stored; says nothing about whether its stages validate."""
    return read_latest_version_raw(project_id) is not None


def load_stage_entries(project_id: str) -> list[StageEntry]:
    """Parsed per stage, so ONE invalid stage is an issue, not an exception."""
    entries: list[StageEntry] = []
    for index, spec in enumerate(read_stage_specs(project_id)):
        label = _label(spec, index)
        try:
            entries.append(StageEntry(label=label, stage=parse_stage(spec)))
        except ValidationError as err:
            entries.append(StageEntry(label=label, issues=format_errors(err)))
    return entries


def load_workflow_object(project_id: str) -> Workflow:
    """Strict: raises WorkflowLoadError when a project has no stages, or invalid ones."""
    entries = load_stage_entries(project_id)
    issues = find_file_issues(entries)
    if not entries:
        issues.append(f"project '{project_id}' has no stages")
    stages = list_parsed_stages(entries)
    issues += validate_workflow(stages)
    if issues:
        raise WorkflowLoadError(f"project {project_id!r} stages", issues)
    return Workflow(stages=stages)


def load_workflow(project_id: str) -> list[Stage]:
    return load_workflow_object(project_id).stages


# ─── Raw specs ───────────────────────────────────────────────────────────────

def read_stage_specs(project_id: str) -> list[JsonDict]:
    """The newest version's stage specs, unvalidated and in order; [] when unversioned."""
    document = read_latest_version_raw(project_id)
    if document is None:
        return []
    stages = document.get("stages")
    return [s for s in stages if isinstance(s, dict)] if isinstance(stages, list) else []


def read_stages_edited_at(project_id: str) -> datetime | None:
    """When the newest version was saved; None for a project that has never had stages."""
    document = read_latest_version_raw(project_id)
    return None if document is None else read_orderable_stamp(document.get("created_at"))


def index_stage_specs_by_id(project_id: str) -> dict[str, JsonDict]:
    """`{id: spec dict}` — the map the stage editor validates a change against."""
    return {stage.id: stage_to_spec_dict(stage) for stage in load_workflow(project_id)}


def _label(spec: JsonDict, index: int) -> str:
    """How an issue names its stage: the id, or the position when there is none."""
    stage_id = spec.get("id")
    return stage_id if isinstance(stage_id, str) and stage_id else f"stage #{index + 1}"


# ─── Source & code reads ─────────────────────────────────────────────────────

def resolve_function_code(stage_def: Stage | None) -> str | None:
    block = stage_def.find_authored_code_block() if stage_def else None
    return block.code if block else None

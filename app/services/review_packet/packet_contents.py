"""A finished packet read back off its own folder — the only facts a README may state,
since a reader on GitHub has this folder and nothing else."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.ids import ID
from app.models.run_manifest import InputBinding
from app.models.stages.stage_base import WorkflowOutputRule
from app.services.review_packet.branch_order import sort_stages_by_branch
from app.services.review_packet.data import (
    DATA_DIR,
    DOCUMENT_FILE,
    MANIFEST_FILE,
    WORKFLOW_FILE,
    build_input_copy_path,
)
from app.services.review_packet.views import RunView, build_run_view


class PacketStageSpec(BaseModel):
    """One `workflow.json` entry, narrowed to what a README states about it."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: ID
    type: str = ""
    description: str = ""
    input_ids: list[ID] = Field(default_factory=list, alias="inputs")
    workflow_outputs: list[WorkflowOutputRule] = Field(default_factory=list)

    @field_validator("input_ids", mode="before")
    @classmethod
    def _read_input_ids(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return []
        return [entry.get("id") if isinstance(entry, dict) else entry for entry in value]

    @field_validator("workflow_outputs", mode="before")
    @classmethod
    def _absent_is_none_declared(cls, value: Any) -> Any:
        return [] if value is None else value


class PacketClaim(BaseModel):
    label: str
    column: str
    stage_id: ID
    primary: bool
    # None where the cell could not be read back; the README says so rather than blanking it.
    value: str | None


class PacketSource(BaseModel):
    binding: InputBinding
    row_count: int | None
    copy_path: str | None


class PacketStep(BaseModel):
    stage_id: ID
    type: str
    description: str
    row_count: int | None
    parent_ids: list[ID]


class PacketFlag(BaseModel):
    stage_id: ID
    severity: str
    column: str | None
    message: str


class PacketContents(BaseModel):
    root: Path
    title: str
    opening: str
    run: RunView
    claims: list[PacketClaim]
    sources: list[PacketSource]
    steps: list[PacketStep]
    flags: list[PacketFlag]
    # False when the run pinned a version the export could not read. `steps` and
    # `claims` are then empty for want of a graph, not because the run had none.
    has_workflow: bool


def read_packet_contents(root: Path) -> PacketContents:
    run = build_run_view(_read_json_object(root / MANIFEST_FILE), None)
    workflow_file = root / WORKFLOW_FILE
    specs = _read_stage_specs(workflow_file)
    row_counts_by_id = {stage.stage_id: stage.row_count for stage in run.stages}
    title, opening = _read_document_opening(root, run)
    return PacketContents(
        root=root,
        title=title,
        opening=opening,
        run=run,
        claims=_read_claims(root, specs),
        sources=_read_sources(root, run, row_counts_by_id),
        steps=_read_steps(specs, row_counts_by_id),
        flags=_read_flags(run),
        has_workflow=workflow_file.is_file(),
    )


def find_data_file(root: Path, stage_id: ID) -> str | None:
    relative = f"{DATA_DIR}/{stage_id}.csv"
    return relative if (root / relative).is_file() else None


def _read_stage_specs(path: Path) -> list[PacketStageSpec]:
    if not path.is_file():
        return []
    dumped = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(dumped, list):
        return []
    return [PacketStageSpec.model_validate(entry) for entry in dumped]


def _read_claims(root: Path, specs: list[PacketStageSpec]) -> list[PacketClaim]:
    return [
        PacketClaim(
            label=rule.label,
            column=rule.column,
            stage_id=spec.id,
            primary=rule.primary,
            value=_read_cell(root, spec.id, rule.column),
        )
        for spec in specs
        for rule in spec.workflow_outputs
    ]


def _read_cell(root: Path, stage_id: ID, column: str) -> str | None:
    relative = find_data_file(root, stage_id)
    if relative is None:
        return None
    with (root / relative).open(newline="", encoding="utf-8", errors="replace") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        row = next(reader, None)
    if row is None or column not in header:
        return None
    cell = row[header.index(column)]
    return cell if cell else None


def _read_sources(
    root: Path, run: RunView, row_counts_by_id: dict[ID, int]
) -> list[PacketSource]:
    return [
        PacketSource(
            binding=binding,
            row_count=row_counts_by_id.get(binding.stage_id),
            copy_path=_find_input_copy(root, binding, index),
        )
        for index, binding in enumerate(run.inputs)
    ]


def _find_input_copy(root: Path, binding: InputBinding, index: int) -> str | None:
    relative = build_input_copy_path(binding, index)
    return relative if (root / relative).is_file() else None


def _read_steps(
    specs: list[PacketStageSpec], row_counts_by_id: dict[ID, int]
) -> list[PacketStep]:
    declared = {spec.id for spec in specs}
    return [
        PacketStep(
            stage_id=spec.id,
            type=spec.type,
            description=spec.description,
            row_count=row_counts_by_id.get(spec.id),
            parent_ids=[up for up in spec.input_ids if up in declared],
        )
        for spec in sort_stages_by_branch(_widest_branch_first(specs, row_counts_by_id))
    ]


def _widest_branch_first(
    specs: list[PacketStageSpec], row_counts_by_id: dict[ID, int]
) -> list[PacketStageSpec]:
    """Where two branches are free at once, the one carrying more rows is the main line."""
    return sorted(specs, key=lambda spec: -_count_rows_out(spec, row_counts_by_id))


def _count_rows_out(spec: PacketStageSpec, row_counts_by_id: dict[ID, int]) -> int:
    """A step the manifest holds no record of sorts last; nothing prints this number."""
    recorded = row_counts_by_id.get(spec.id)
    return 0 if recorded is None else recorded


def _read_flags(run: RunView) -> list[PacketFlag]:
    return [
        PacketFlag(
            stage_id=stage.stage_id,
            severity=issue.severity,
            column=issue.column,
            message=issue.message,
        )
        for stage in run.stages
        for validation in stage.validations
        for issue in validation.issues
    ]


def _read_document_opening(root: Path, run: RunView) -> tuple[str, str]:
    named = run.project or run.run_id
    path = root / DOCUMENT_FILE
    if not path.is_file():
        return named, ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    headings = [line for line in lines if line.startswith("#")]
    title = headings[0].lstrip("#").strip() if headings else ""
    return title or named, _read_first_paragraph(lines)


def _read_first_paragraph(lines: list[str]) -> str:
    """A markdown paragraph is hard-wrapped, so it runs to the blank line after it."""
    paragraph: list[str] = []
    past_heading = False
    for line in lines:
        if line.startswith("#"):
            past_heading = True
        elif past_heading and line.strip():
            paragraph.append(line.strip())
        elif paragraph:
            break
    return " ".join(paragraph)


def _read_json_object(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return loaded

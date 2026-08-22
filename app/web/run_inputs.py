"""What the run form's file pickers are built from: one row per file input the chosen
version declares, and the project's stored files every row may pick from."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.core import files as file_store
from app.models.run_parameters import RunParameters
from app.models.schema import StageId
from app.runtime.manifest import RunManifest
from app.web.file_sizes import describe_bytes
from app.web.loading import list_file_inputs

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class FileChoice(BaseModel):
    # The value the picker submits: the record, not the bytes it holds.
    file_id: str
    filename: str
    bytes: int
    uploaded_at: datetime
    label: str
    uploaded_label: str
    size_label: str


class UploadedFileChoice(FileChoice):
    ok: Literal[True] = True
    path: str


class InputRow(BaseModel):
    stage_id: str
    # What the workflow itself names, if anything. A row that has one runs without a
    # pick; a row that does not cannot run until something is chosen.
    authored_path: str
    # Set only by a duplicate: the files and row cap the run being copied used.
    selected_file_ids: list[str] = []
    limit: int | None = None


class RunInputChoices(BaseModel):
    inputs: list[InputRow]
    # Project-wide, not per row: a file is not tied to the step it was first read by,
    # so every row offers the same list and the reader picks.
    files: list[FileChoice]
    # A duplicate's row caps on stages with no row here; the form carries them hidden.
    carried_limits: dict[StageId, int] = {}
    bust_cache: bool = False


def build_run_input_choices(
    project_id: str, version_id: str | None = None, copy_of: RunManifest | None = None
) -> RunInputChoices:
    params = copy_of.parameters if copy_of else RunParameters()
    picks = _match_recorded_files(project_id, params)
    rows = [InputRow(stage_id=row["stage_id"], authored_path=row["path"],
                     selected_file_ids=picks.get(row["stage_id"], []),
                     limit=params.limits.get(row["stage_id"]))
            for row in list_file_inputs(project_id, version_id)]
    shown = {row.stage_id for row in rows}
    return RunInputChoices(
        inputs=rows,
        files=[build_file_choice(record)
               for record in file_store.list_project_files(project_id)],
        carried_limits={stage_id: cap for stage_id, cap in params.limits.items()
                        if stage_id not in shown},
        bust_cache=params.bust_cache,
    )


def _match_recorded_files(
    project_id: str, params: RunParameters
) -> dict[StageId, list[str]]:
    """A binding records the paths it read; the picker's values are the records owning them."""
    id_by_key: dict[str, str] = {}
    for record in file_store.list_project_files(project_id):
        for key in _name_the_record(record):
            id_by_key[key] = record.id
    picks: dict[StageId, list[str]] = {}
    for stage_id, override in params.run_bindings.items():
        chosen = [file_id for path in (dict(override).get("paths") or [])
                  # A file the project no longer holds cannot be pre-picked.
                  if (file_id := _match_one_path(id_by_key, path)) is not None]
        if chosen:
            picks[stage_id] = chosen
    return picks


def _match_one_path(id_by_key: dict[str, str], path: object) -> str | None:
    if not isinstance(path, str):
        return None
    return id_by_key.get(path) or id_by_key.get(Path(path).parent.name)


def _name_the_record(record: file_store.ProjectFile) -> tuple[str, str, str]:
    # A file's directory was keyed by sha256 before it was keyed by record id.
    return (str(file_store.resolve_stored_path(record)), record.id, record.sha256)


def build_file_choice(record: file_store.ProjectFile) -> FileChoice:
    uploaded_at = datetime.fromisoformat(record.created_at)
    uploaded_label = f"Uploaded {format_upload_time(uploaded_at)}"
    size_label = describe_bytes(record.byte_count)
    return FileChoice(
        file_id=record.id,
        filename=record.filename,
        bytes=record.byte_count,
        uploaded_at=uploaded_at,
        label=f"{uploaded_label} · {record.filename} · {size_label}",
        uploaded_label=uploaded_label,
        size_label=size_label,
    )


def build_uploaded_file_choice(record: file_store.ProjectFile) -> UploadedFileChoice:
    choice = build_file_choice(record)
    return UploadedFileChoice(
        **choice.model_dump(),
        path=str(file_store.resolve_stored_path(record)),
    )


def format_upload_time(uploaded_at: datetime) -> str:
    return (f"{uploaded_at.day} {_MONTHS[uploaded_at.month - 1]} {uploaded_at.year}, "
            f"{uploaded_at:%H:%M}")

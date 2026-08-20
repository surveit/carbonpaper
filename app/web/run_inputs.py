"""What the run form's file pickers are built from: one row per file input the chosen
version declares, and the project's stored files every row may pick from."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.models.run_parameters import RunParameters
from app.models.schema import StageId, TypeUnsafeUserStageConfigOverride
from app.runtime.manifest import RunManifest
from app.services import uploads
from app.web.file_sizes import describe_bytes
from app.web.loading import list_file_inputs

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class FileChoice(BaseModel):
    sha256: str
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
    # Set only by a duplicate: the file and row cap the run being copied used.
    selected_sha256: str | None = None
    limit: int | None = None


class CopiedRun(BaseModel):
    """What a duplicate carried over from the run it opened on — and what it could not."""

    run_id: str
    version_id: str | None
    picks: int
    limits: int
    bust_cache: bool
    # The form launches production runs only, so duplicating a test run changes the
    # run's kind: this one writes the stage cache and halts on a review queue.
    was_test_run: bool
    # Stage ids whose recorded file is no longer among the project's files. Their rows
    # open on the workflow's authored path, which is a DIFFERENT input than the copied
    # run read — so the form says so rather than looking pre-filled.
    files_not_copied: list[StageId]
    # Row caps the copied run set on stages this form has no field for: it caps file
    # inputs only. Carrying them as hidden fields would apply a cap the reader cannot
    # see, so they are named instead.
    limits_not_copied: dict[StageId, int]


class RunInputChoices(BaseModel):
    inputs: list[InputRow]
    # Project-wide, not per row: a file is not tied to the step it was first read by,
    # so every row offers the same list and the reader picks.
    files: list[FileChoice]
    copied: CopiedRun | None = None


def build_run_input_choices(
    project_id: str, version_id: str | None = None, copy_of: RunManifest | None = None
) -> RunInputChoices:
    params = copy_of.parameters if copy_of else RunParameters()
    picks, unmatched = _match_recorded_files(project_id, params)
    rows = [InputRow(stage_id=row["stage_id"], authored_path=row["path"],
                     selected_sha256=picks.get(row["stage_id"]),
                     limit=params.limits.get(row["stage_id"]))
            for row in list_file_inputs(project_id, version_id)]
    return RunInputChoices(
        inputs=rows,
        files=[build_file_choice(record)
               for record in uploads.list_project_files(project_id)],
        copied=_describe_copy(copy_of, rows, picks, unmatched) if copy_of else None,
    )


def _describe_copy(
    record: RunManifest, rows: list[InputRow], picks: dict[StageId, str],
    unmatched: list[StageId],
) -> CopiedRun:
    shown = {row.stage_id for row in rows}
    return CopiedRun(
        run_id=record.run_id,
        version_id=record.workflow_version,
        picks=len([row for row in rows if row.selected_sha256]),
        limits=len([row for row in rows if row.limit is not None]),
        bust_cache=record.parameters.bust_cache,
        was_test_run=record.parameters.is_test_run,
        # A pick for a stage this version has no row for is not copied either: the
        # version picker may have moved off the one the run pinned.
        files_not_copied=unmatched + [s for s in picks if s not in shown],
        limits_not_copied={stage_id: cap
                           for stage_id, cap in record.parameters.limits.items()
                           if stage_id not in shown},
    )


def _match_recorded_files(
    project_id: str, params: RunParameters
) -> tuple[dict[StageId, str], list[StageId]]:
    """A binding records the stored PATH it read; the picker's value is a sha256."""
    sha_by_path = {str(uploads.resolve_stored_path(record)): record.sha256
                   for record in uploads.list_project_files(project_id)}
    picks: dict[StageId, str] = {}
    unmatched: list[StageId] = []
    for stage_id, override in params.run_bindings.items():
        sha256 = sha_by_path.get(_read_bound_path(override))
        if sha256 is None:
            unmatched.append(stage_id)
        else:
            picks[stage_id] = sha256
    return picks, unmatched


def _read_bound_path(override: TypeUnsafeUserStageConfigOverride) -> str:
    path = override.get("path")
    return str(path) if isinstance(path, str) else ""


def build_file_choice(record: uploads.UploadedFile) -> FileChoice:
    uploaded_at = datetime.fromisoformat(record.created_at)
    uploaded_label = f"Uploaded {format_upload_time(uploaded_at)}"
    size_label = describe_bytes(record.byte_count)
    return FileChoice(
        sha256=record.sha256,
        filename=record.filename,
        bytes=record.byte_count,
        uploaded_at=uploaded_at,
        label=f"{uploaded_label} · {record.filename} · {size_label}",
        uploaded_label=uploaded_label,
        size_label=size_label,
    )


def build_uploaded_file_choice(record: uploads.UploadedFile) -> UploadedFileChoice:
    choice = build_file_choice(record)
    return UploadedFileChoice(
        **choice.model_dump(),
        path=str(uploads.resolve_stored_path(record)),
    )


def format_upload_time(uploaded_at: datetime) -> str:
    return (f"{uploaded_at.day} {_MONTHS[uploaded_at.month - 1]} {uploaded_at.year}, "
            f"{uploaded_at:%H:%M}")

"""What the run form's file pickers are built from: one row per file input the chosen
version declares, and the project's stored files every row may pick from."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, computed_field

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

    @computed_field
    @property
    def label(self) -> str:
        return (f"Uploaded {format_upload_time(self.uploaded_at)} · {self.filename} · "
                f"{describe_bytes(self.bytes)}")


class UploadedFileChoice(FileChoice):
    ok: Literal[True] = True
    path: str


class InputRow(BaseModel):
    stage_id: str
    # What the workflow itself names, if anything. A row that has one runs without a
    # pick; a row that does not cannot run until something is chosen.
    authored_path: str


class RunInputChoices(BaseModel):
    inputs: list[InputRow]
    # Project-wide, not per row: a file is not tied to the step it was first read by,
    # so every row offers the same list and the reader picks.
    files: list[FileChoice]


def build_run_input_choices(project_id: str, version_id: str | None = None) -> RunInputChoices:
    return RunInputChoices(
        inputs=[InputRow(stage_id=row["stage_id"], authored_path=row["path"])
                for row in list_file_inputs(project_id, version_id)],
        files=[build_file_choice(record)
               for record in uploads.list_project_files(project_id)],
    )


def build_file_choice(record: uploads.UploadedFile) -> FileChoice:
    return FileChoice(
        sha256=record.sha256,
        filename=record.filename,
        bytes=record.byte_count,
        uploaded_at=record.created_at,
    )


def build_uploaded_file_choice(record: uploads.UploadedFile) -> UploadedFileChoice:
    choice = build_file_choice(record)
    return UploadedFileChoice(
        **choice.model_dump(exclude={"label"}),
        path=str(uploads.resolve_stored_path(record)),
    )


def format_upload_time(uploaded_at: datetime) -> str:
    return (f"{uploaded_at.day} {_MONTHS[uploaded_at.month - 1]} {uploaded_at.year}, "
            f"{uploaded_at:%H:%M}")

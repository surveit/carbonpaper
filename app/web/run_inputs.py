"""What the run form's file pickers are built from: one row per file input the chosen
version declares, and the project's stored files every row may pick from."""
from __future__ import annotations

from pydantic import BaseModel

from app.services import uploads
from app.web.loading import list_file_inputs


class FileChoice(BaseModel):
    sha256: str
    filename: str
    bytes: int


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
        files=[FileChoice(sha256=record.sha256, filename=record.filename,
                          bytes=record.byte_count)
               for record in uploads.list_project_files(project_id)],
    )

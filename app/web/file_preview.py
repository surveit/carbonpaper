"""A bounded table view of one stored project file."""
from __future__ import annotations

from pydantic import BaseModel

from app.services.frame_profile import read_stored_file_frame
from app.web.loading import PREVIEW_ROWS_SHOWN, render_frame_as_text


class FilePreview(BaseModel):
    filename: str
    format: str
    columns: list[str]
    rows: list[list[str]]
    row_count: int


def build_file_preview(project_id: str, sha256: str) -> FilePreview:
    stored = read_stored_file_frame(project_id, sha256)
    frame = stored.frame
    shown = render_frame_as_text(frame.head(PREVIEW_ROWS_SHOWN))
    return FilePreview(
        filename=stored.filename,
        format=stored.format.value,
        columns=[str(column) for column in shown.columns],
        rows=[[str(cell) for cell in row]
              for row in shown.itertuples(index=False, name=None)],
        row_count=len(frame),
    )

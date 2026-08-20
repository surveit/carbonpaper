"""The connector params a run binds for a file the store holds."""
from __future__ import annotations

from app.core.files import open_project_file
from app.core.source_files import resolve_file_format
from app.models.schema import TypeUnsafeUserStageConfigOverride


def resolve_file_binding(project_id: str, file_id: str) -> TypeUnsafeUserStageConfigOverride:
    """The connector params a run of `project_id` binds for one of its files."""
    record, path = open_project_file(project_id, file_id)
    return {"path": str(path), "format": resolve_file_format(record.filename).value}

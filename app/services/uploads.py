"""The connector params a run binds for the files the store holds."""
from __future__ import annotations

from collections.abc import Sequence

from app.core.files import open_project_file
from app.core.source_files import FileFormat, resolve_file_format
from app.models.schema import TypeUnsafeUserStageConfigOverride


def resolve_files_binding(
    project_id: str, file_ids: Sequence[str]
) -> TypeUnsafeUserStageConfigOverride:
    """The files an input reads this run, in the order given; they become one table."""
    if not file_ids:
        raise ValueError("a file binding names at least one file, and this one names none")
    opened = [open_project_file(project_id, file_id) for file_id in file_ids]
    return {
        "paths": [str(path) for _record, path in opened],
        "format": _one_shared_format([record.filename for record, _path in opened]).value,
    }


def _one_shared_format(filenames: Sequence[str]) -> FileFormat:
    """Read as one table, so one reader — a mixed set is refused rather than half-read."""
    by_format: dict[FileFormat, list[str]] = {}
    for filename in filenames:
        by_format.setdefault(resolve_file_format(filename), []).append(filename)
    if len(by_format) > 1:
        raise ValueError(
            "files read as one table must share a format, and these do not: "
            + "; ".join(f"{fmt.value} — {', '.join(names)}"
                        for fmt, names in sorted(by_format.items()))
        )
    return next(iter(by_format))

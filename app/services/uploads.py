"""The connector params a run binds for the file(s) the store holds."""
from __future__ import annotations

from collections.abc import Sequence

from app.core.files import open_project_file
from app.core.source_files import FileFormat, resolve_file_format
from app.models.schema import TypeUnsafeUserStageConfigOverride


def resolve_file_binding(project_id: str, file_id: str) -> TypeUnsafeUserStageConfigOverride:
    """The connector params a run of `project_id` binds for one of its files."""
    record, path = open_project_file(project_id, file_id)
    return {"path": str(path), "format": resolve_file_format(record.filename).value}


def resolve_files_binding(
    project_id: str, file_ids: Sequence[str]
) -> TypeUnsafeUserStageConfigOverride:
    """Several files read as one table. One file keeps `path`, so its cache key does not move."""
    if not file_ids:
        raise ValueError("a file binding names at least one file, and this one names none")
    if len(file_ids) == 1:
        return resolve_file_binding(project_id, file_ids[0])
    opened = [open_project_file(project_id, file_id) for file_id in file_ids]
    return {
        # None takes an authored single `path` back off, which the model refuses to
        # hold beside `paths`. A one-file binding keeps emitting `path` and nothing
        # else, so its stage fingerprint — and the cache under it — does not move.
        "path": None,
        "paths": [str(path) for _record, path in opened],
        "format": _one_format([record.filename for record, _path in opened]).value,
    }


def _one_format(filenames: Sequence[str]) -> FileFormat:
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

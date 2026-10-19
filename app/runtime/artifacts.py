"""Where a report stage's files go, and the only way sandboxed code writes one."""
from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pyarrow as pa

from .context import RunContext

# The column the emitter's own output frame carries: one row per file written.
WRITTEN_FILE_COLUMN = "file"


def prepare_artifact_dir(destination: str | None, ctx: RunContext) -> Path:
    output_dir = ctx.require_run_dir() / "artifacts" / Path(destination or "build/").name
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


class ArtifactEmitter:
    """Confines every write to one directory, and remembers what was written."""

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir
        self._written: list[str] = []

    def emit_file(self, name: str, text: str) -> str:
        path = self._open_for_write(name)
        path.write_text(text, encoding="utf-8")
        return self._record(name)

    def emit_table(self, name: str, rows: Sequence[Mapping[str, Any]]) -> str:
        path = self._open_for_write(name)
        columns = list(rows[0]) if rows else []
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for ordinal, row in enumerate(rows):
                _require_same_columns(name, ordinal, list(row), columns)
                writer.writerow(dict(row))
        return self._record(name)

    def list_written_files(self) -> pa.Table:
        return pa.table({WRITTEN_FILE_COLUMN: pa.array(self._written, type=pa.string())})

    def _open_for_write(self, name: str) -> Path:
        path = self._resolve_inside(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _resolve_inside(self, name: str) -> Path:
        root = self._output_dir.resolve()
        path = (root / name).resolve()
        if path == root or not path.is_relative_to(root):
            raise ValueError(
                f"cannot write {name!r}: a report writes only inside its own output "
                f"directory, so the name must be a plain relative filename"
            )
        return path

    def _record(self, name: str) -> str:
        self._written.append(name)
        return name


def _require_same_columns(
    name: str, ordinal: int, columns: list[str], expected: list[str]
) -> None:
    if columns == expected:
        return
    raise ValueError(
        f"cannot write {name!r}: its columns come from the first row, {expected}, but row "
        f"{ordinal} carries {columns} — every row must name the same columns, or the "
        f"written file would put one row's values under another's header"
    )

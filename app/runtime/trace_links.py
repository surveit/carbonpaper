"""Provenance links a published artifact can carry: write each row's trace page
into the artifact bundle and hand back a relative href to it."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from app.core.errors import (
    RowOutOfRange,
    StageNotInRun,
    TraceRowMismatch,
    TraceUnavailableError,
)
from app.models import Stage

from .trace import trace_row, trace_to_dict
from .trace_assets import copy_trace_assets
from .trace_page import render_standalone_trace_page
from .trace_view import build_trace_view

TRACE_ROW_ORDINAL_COLUMN = "trace_row_ordinal"

_TRACES_DIR = "_traces"
_ASSETS_DIR = "_assets"


@dataclass(frozen=True)
class RowTraceExporter:
    run_dir: Path
    output_dir: Path  # the publish stage's artifacts root
    stages: dict[str, Stage]

    def export_row_trace(
        self, stage_id: str, row_ordinal: int, from_file: Path, *, row: Mapping[str, Any]
    ) -> str:
        """`from_file` is the file the caller is writing — only it knows its own
        depth in the bundle. `row` is the row being rendered, which the ordinal
        is checked against; both must be given, since either alone can be wrong."""
        if row_ordinal < 0:
            raise ValueError(f"row_ordinal must be >= 0, got {row_ordinal}")
        _reject_contradicted_ordinal(stage_id, row_ordinal, row)
        from_dir = self._locate_writer_dir(from_file)
        page = self._locate_page(stage_id, row_ordinal)
        if not page.is_file():
            self._write_page(page, self._build_view(stage_id, row_ordinal))
            copy_trace_assets(self._resolve_root() / _ASSETS_DIR)
        return _build_relative_href(page, from_dir)

    def _locate_writer_dir(self, from_file: Path) -> Path:
        """An href from outside the bundle has to climb out of it, which resolves
        in place and dies the moment the bundle is copied — the exact failure
        this export exists to prevent."""
        root = self._resolve_root()
        from_dir = from_file.resolve().parent
        if not from_dir.is_relative_to(root):
            raise ValueError(
                f"from_file {from_file} is outside the artifacts root {self.output_dir}")
        return from_dir

    def _locate_page(self, stage_id: str, row_ordinal: int) -> Path:
        # quote() keeps a stage id carrying separators or `..` inside the bundle.
        return self._resolve_root() / _TRACES_DIR / quote(stage_id, safe="") / f"{row_ordinal}.html"

    def _resolve_root(self) -> Path:
        return self.output_dir.resolve()

    def _build_view(self, stage_id: str, row_ordinal: int) -> dict[str, Any]:
        """A trace that stopped short of its origin (fan-in, a missing output) is
        unavailable, not partial: the reader would be shown a provenance chain
        that doesn't reach the source."""
        try:
            trace = trace_row(self.run_dir, stage_id, row_ordinal)
        except (StageNotInRun, RowOutOfRange, FileNotFoundError) as exc:
            raise TraceUnavailableError(
                f"stage {stage_id!r} row {row_ordinal}: {exc}") from exc
        if not trace.end.reached_origin:
            raise TraceUnavailableError(
                f"stage {stage_id!r} row {row_ordinal}: {trace.end.message}")
        return build_trace_view(trace_to_dict(trace), self.stages)

    def _write_page(self, page: Path, view: dict[str, Any]) -> None:
        page.parent.mkdir(parents=True, exist_ok=True)
        prefix = _build_relative_href(self._resolve_root() / _ASSETS_DIR, page.parent) + "/"
        page.write_text(render_standalone_trace_page(view, prefix), encoding="utf-8")


def _reject_contradicted_ordinal(
    stage_id: str, row_ordinal: int, row: Mapping[str, Any]
) -> None:
    """The runtime stamps each row with its true on-disk position before publish
    runs, so the row itself is the authority on which ordinal belongs to it."""
    if TRACE_ROW_ORDINAL_COLUMN not in row:
        raise TraceRowMismatch(
            f"stage {stage_id!r} row_ordinal {row_ordinal}: the row passed carries no "
            f"{TRACE_ROW_ORDINAL_COLUMN!r} column. Pass a row from the frame this publish "
            "function was given, keeping that column."
        )
    carried = row[TRACE_ROW_ORDINAL_COLUMN]
    if carried != row_ordinal:
        raise TraceRowMismatch(
            f"stage {stage_id!r}: row_ordinal {row_ordinal} was passed for the row whose "
            f"{TRACE_ROW_ORDINAL_COLUMN} is {carried}. The frame has been reordered or "
            f"filtered, so use row[{TRACE_ROW_ORDINAL_COLUMN!r}] rather than enumerate()."
        )


def _build_relative_href(target: Path, from_dir: Path) -> str:
    """POSIX separators: os.path.relpath yields backslashes on Windows, which a
    browser reads as literal characters rather than path separators."""
    return Path(os.path.relpath(target, from_dir)).as_posix()


__all__ = ["RowTraceExporter", "TRACE_ROW_ORDINAL_COLUMN"]

"""Provenance links a published artifact can carry: write each row's trace page
into the artifact bundle and hand back a relative href to it."""
from __future__ import annotations

import operator
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from app.core.errors import (
    RowOutOfRange,
    StageNotInRun,
    TraceRowNotStamped,
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

    def export_row_trace(self, stage_id: str, from_file: Path, *, row: Mapping[str, Any]) -> str:
        """Takes no ordinal: `row` carries its own, so no caller can name a
        position that isn't this row's. `from_file` is the file being written —
        only it knows its own depth in the bundle."""
        row_ordinal = _read_stamped_ordinal(stage_id, row)
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


def _read_stamped_ordinal(stage_id: str, row: Mapping[str, Any]) -> int:
    """The runtime stamps every publish input row with its true position in the
    stage's output, so the row is the only authority on its own ordinal."""
    try:
        ordinal = operator.index(row[TRACE_ROW_ORDINAL_COLUMN])
    except (KeyError, TypeError) as exc:
        raise TraceRowNotStamped(_describe_unstamped_row(stage_id, row)) from exc
    if ordinal < 0:
        raise TraceRowNotStamped(_describe_unstamped_row(stage_id, row))
    return ordinal


def _describe_unstamped_row(stage_id: str, row: Mapping[str, Any]) -> str:
    return (
        f"stage {stage_id!r}: the row passed carries no usable "
        f"{TRACE_ROW_ORDINAL_COLUMN!r} (got {row.get(TRACE_ROW_ORDINAL_COLUMN)!r}). The "
        "runtime stamps it onto every row of a publish input — sort and filter freely, but "
        "pass a row from that frame rather than one built by hand."
    )


def _build_relative_href(target: Path, from_dir: Path) -> str:
    """POSIX separators: os.path.relpath yields backslashes on Windows, which a
    browser reads as literal characters rather than path separators."""
    return Path(os.path.relpath(target, from_dir)).as_posix()


__all__ = ["RowTraceExporter", "TRACE_ROW_ORDINAL_COLUMN"]

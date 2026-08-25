"""One file per stage saying how each output row came to be."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa

from app.core.frames import read_frame_table, write_frame_table

from .branches import BRANCHES_KEY, RowBranches
from .errors import RowSidecarLengthMismatch
from .lineage import TRACE_SOURCE_STAGE_KEY, RowLineage


@dataclass(frozen=True)
class RowSidecar:
    """Both halves are optional and independent; None is "the run recorded none"."""

    lineage: RowLineage | None = None
    branches: RowBranches | None = None


def resolve_row_sidecar_path(run_dir: Path, stage_id: str) -> Path:
    return Path(run_dir) / "outputs" / f"{stage_id}.lineage.parquet"


def write_row_sidecar(
    run_dir: Path, stage_id: str, lineage: RowLineage | None, branches: RowBranches | None
) -> None:
    if lineage is None and branches is None:
        return
    if lineage is not None and branches is not None and len(lineage) != len(branches):
        raise RowSidecarLengthMismatch(
            f"{stage_id!r}: {len(lineage)} row(s) of lineage against "
            f"{len(branches)} of branches — both are keyed by output row"
        )
    write_frame_table(_joined_table(lineage, branches), resolve_row_sidecar_path(run_dir, stage_id))


def read_row_sidecar(run_dir: Path, stage_id: str) -> RowSidecar:
    table = _read_table(resolve_row_sidecar_path(run_dir, stage_id))
    branches = _read_branches(table)
    if branches is None:
        branches = _read_branches(_read_table(_pre_merge_branch_path(run_dir, stage_id)))
    return RowSidecar(lineage=_read_lineage(table), branches=branches)


def _joined_table(lineage: RowLineage | None, branches: RowBranches | None) -> pa.Table:
    halves = [half.to_table() for half in (lineage, branches) if half is not None]
    return pa.Table.from_arrays(
        [column for half in halves for column in half.columns],
        schema=pa.schema([field for half in halves for field in half.schema]),
    )


def _read_lineage(table: pa.Table | None) -> RowLineage | None:
    # A branches-only sidecar reports no lineage, not a row from nowhere.
    if table is None or TRACE_SOURCE_STAGE_KEY not in table.column_names:
        return None
    return RowLineage.from_table(table)


def _read_branches(table: pa.Table | None) -> RowBranches | None:
    if table is None or BRANCHES_KEY not in table.column_names:
        return None
    return RowBranches.from_table(table)


def _read_table(path: Path) -> pa.Table | None:
    return read_frame_table(path) if path.exists() else None


# A run made before the two sidecars became one left its branches in a sibling.

# https://github.com/surveit/carbonpaper/issues/880
def _pre_merge_branch_path(run_dir: Path, stage_id: str) -> Path:
    return Path(run_dir) / "outputs" / f"{stage_id}.branch.parquet"

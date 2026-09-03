"""One file per stage saying how each output row came to be."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa

from app.core.frames import read_frame_column_names, read_frame_table, write_frame_table

from .branches import BRANCHES_KEY, RowBranches
from .errors import LineageSidecarLengthMismatch
from .lineage import LINEAGE_KEYS, TRACE_SOURCE_STAGE_KEY, RowLineage


@dataclass(frozen=True)
class LineageSidecar:
    """Both halves are optional and independent; None is "the run recorded none"."""

    lineage: RowLineage | None = None
    branches: RowBranches | None = None


def resolve_lineage_sidecar_path(run_dir: Path, stage_id: str) -> Path:
    return Path(run_dir) / "outputs" / f"{stage_id}.lineage.parquet"


def write_lineage_sidecar(
    run_dir: Path, stage_id: str, lineage: RowLineage | None, branches: RowBranches | None
) -> None:
    if lineage is None and branches is None:
        return
    if lineage is not None and branches is not None and len(lineage) != len(branches):
        raise LineageSidecarLengthMismatch(
            f"{stage_id!r}: {len(lineage)} row(s) of lineage against "
            f"{len(branches)} of branches — both are keyed by output row"
        )
    write_frame_table(
        _joined_table(lineage, branches), resolve_lineage_sidecar_path(run_dir, stage_id))


def read_lineage_sidecar(run_dir: Path, stage_id: str) -> LineageSidecar:
    table = _read_table(resolve_lineage_sidecar_path(run_dir, stage_id))
    branches = _read_branches(table)
    if branches is None:
        branches = _read_branches(_read_table(_pre_merge_branch_path(run_dir, stage_id)))
    return LineageSidecar(lineage=_read_lineage(table), branches=branches)


def read_row_lineage(run_dir: Path, stage_id: str) -> RowLineage | None:
    """As `read_lineage_sidecar` for a caller wanting no branches: `_branches` stays undecoded."""
    path = resolve_lineage_sidecar_path(run_dir, stage_id)
    if not path.exists():
        return None
    held = [key for key in LINEAGE_KEYS if key in read_frame_column_names(path)]
    if TRACE_SOURCE_STAGE_KEY not in held:
        return None
    return RowLineage.from_table(read_frame_table(path, columns=held))


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

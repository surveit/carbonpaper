"""A run's branches on disk, read back one stage at a time. docs/branch-analysis.md"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from functools import partial
from pathlib import Path
from typing import TypeVar

import pyarrow as pa
from pydantic import BaseModel

from app.core.frames import read_frame_table, write_frame_table
from app.models.branch_analysis import BranchId, BranchOption, BranchPath, RowOrdinal
from app.models.schema import StageId
from app.models.workflow_stage import WorkflowStage
from app.runtime.branch_analysis.run_branches import (
    WorkflowRunBranches,
    reconstruct_run_branches,
)
from app.runtime.lineage_sidecar import read_row_lineage

_CACHE_DIR = "branches"
_STAMP_FILE = "built_from.parquet"
_OPTIONS_FILE = "options.parquet"
_ROW_COUNTS_FILE = "rows_per_branch.parquet"

_PATH_KEY = "path"
_ROW_KEY = "row_ordinal"
_BRANCHES_KEY = "branches"
_BRANCH_KEY = "branch_id"
_VERSION_KEY = "pinned_version_id"
_STAGE_KEY = "stage_id"
_COUNT_KEY = "row_count"

# Pinned: left to infer, a stage whose every row took nothing types its column `null`.
_PATHS_SCHEMA = pa.schema([(_PATH_KEY, pa.list_(pa.string()))])
_MERGES_SCHEMA = pa.schema([(_ROW_KEY, pa.int64()), (_BRANCHES_KEY, pa.list_(pa.string()))])
_ROW_COUNTS_SCHEMA = pa.schema([(_BRANCH_KEY, pa.string()), (_COUNT_KEY, pa.int64())])
_STAMP_SCHEMA = pa.schema([(_VERSION_KEY, pa.string()),
                           (_STAGE_KEY, pa.list_(pa.string())),
                           (_COUNT_KEY, pa.list_(pa.int64()))])

_OPTION_FIELDS = tuple(BranchOption.model_fields)

_V = TypeVar("_V")


class StageFrameSize(BaseModel):
    """One stage's frame, the size the analysis read it at."""

    stage_id: StageId
    row_count: int


class BranchCacheStamp(BaseModel):
    """What the analysis was read from. Anything else on disk describes a different run."""

    pinned_version_id: str
    frame_sizes: list[StageFrameSize]

    def list_stage_ids(self) -> list[StageId]:
        return [size.stage_id for size in self.frame_sizes]

    def index_row_counts_by_stage(self) -> dict[StageId, int]:
        return {size.stage_id: size.row_count for size in self.frame_sizes}


def load_run_branches(
    run_dir: Path, stages: dict[StageId, WorkflowStage],
    ordered_stage_ids: list[StageId], row_counts: dict[StageId, int],
    pinned_version_id: str,
) -> WorkflowRunBranches:
    stamp = BranchCacheStamp(
        pinned_version_id=pinned_version_id,
        frame_sizes=[StageFrameSize(stage_id=stage_id, row_count=row_counts[stage_id])
                     for stage_id in ordered_stage_ids])
    held = read_branch_cache(run_dir, stamp, stages)
    if held is not None:
        return held
    worked_out = reconstruct_run_branches(run_dir, stages, ordered_stage_ids, row_counts)
    write_branch_cache(run_dir, stamp, worked_out)
    return worked_out


def write_branch_cache(run_dir: Path, stamp: BranchCacheStamp,
                       run_branches: WorkflowRunBranches) -> None:
    directory = _resolve_cache_dir(run_dir)
    directory.mkdir(parents=True, exist_ok=True)
    _write_options(directory / _OPTIONS_FILE, run_branches.branch_options)
    _write_row_counts(directory / _ROW_COUNTS_FILE, run_branches.row_count_per_branch_id)
    for stage_id, paths in run_branches.branch_paths.items():
        _write_paths(directory, stage_id, paths)
    for stage_id, per_row in run_branches.merges_per_row.items():
        _write_merges(directory, stage_id, per_row)
    # Last, so a write that stops short leaves a cache no reader will claim.
    _write_stamp(directory / _STAMP_FILE, stamp)


def read_branch_cache(run_dir: Path, stamp: BranchCacheStamp,
                      stages: dict[StageId, WorkflowStage]) -> WorkflowRunBranches | None:
    directory = _resolve_cache_dir(run_dir)
    if _read_stamp(directory / _STAMP_FILE) != stamp:
        return None
    stage_ids = stamp.list_stage_ids()
    return WorkflowRunBranches(
        branch_options=_read_options(directory / _OPTIONS_FILE),
        branch_paths=_read_each_stage_once(
            _find_stages_with_a_file(directory, stage_ids,
                                     _resolve_paths_file),
            partial(_read_paths, directory)),
        row_count_per_branch_id=_read_row_counts(directory / _ROW_COUNTS_FILE),
        merges_per_row=_read_each_stage_once(
            _find_stages_with_a_file(directory, stage_ids,
                                     _resolve_merges_file),
            partial(_read_merges, directory)),
        # The sidecars already hold this half, so the cache writes none of it.
        lineages=_read_each_stage_once(stage_ids,
                                       partial(read_row_lineage, run_dir)),
        stages=stages,
        ordered_stage_ids=stage_ids,
        row_counts=stamp.index_row_counts_by_stage(),
    )


class _ReadEachStageOnce(Mapping[StageId, _V]):
    """A stage's file is read the first time a caller names that stage, then held."""

    def __init__(self, stage_ids: Sequence[StageId],
                 read_one: Callable[[StageId], _V]) -> None:
        self._stage_ids = list(stage_ids)
        self._read_one = read_one
        self._held: dict[StageId, _V] = {}

    def __getitem__(self, stage_id: StageId) -> _V:
        if stage_id not in self._held:
            if stage_id not in self._stage_ids:
                raise KeyError(stage_id)
            self._held[stage_id] = self._read_one(stage_id)
        return self._held[stage_id]

    def __iter__(self) -> Iterator[StageId]:
        return iter(self._stage_ids)

    def __len__(self) -> int:
        return len(self._stage_ids)


def _read_each_stage_once(stage_ids: Sequence[StageId],
                          read_one: Callable[[StageId], _V]) -> Mapping[StageId, _V]:
    return _ReadEachStageOnce(stage_ids, read_one)


def _find_stages_with_a_file(directory: Path, ordered_stage_ids: Sequence[StageId],
                             resolve: Callable[[Path, StageId], Path]) -> list[StageId]:
    return [stage_id for stage_id in ordered_stage_ids
            if resolve(directory, stage_id).exists()]


# ─── the files ───────────────────────────────────────────────────────────────

def _write_paths(directory: Path, stage_id: StageId, paths: Sequence[BranchPath]) -> None:
    # Arrow reads the tuples as they are; rebuilding them as lists costs 6x the table.
    write_frame_table(pa.table({_PATH_KEY: list(paths)}, schema=_PATHS_SCHEMA),
                      _resolve_paths_file(directory, stage_id))


def _read_paths(directory: Path, stage_id: StageId) -> list[BranchPath]:
    table = read_frame_table(_resolve_paths_file(directory, stage_id))
    return [tuple(cell or ()) for cell in table.column(_PATH_KEY).to_pylist()]


def _write_merges(directory: Path, stage_id: StageId,
                  per_row: Mapping[RowOrdinal, tuple[BranchId, ...]]) -> None:
    ordinals = sorted(per_row)
    write_frame_table(
        pa.table({_ROW_KEY: ordinals,
                  _BRANCHES_KEY: [per_row[ordinal] for ordinal in ordinals]},
                 schema=_MERGES_SCHEMA),
        _resolve_merges_file(directory, stage_id))


def _read_merges(directory: Path,
                 stage_id: StageId) -> Mapping[RowOrdinal, tuple[BranchId, ...]]:
    table = read_frame_table(_resolve_merges_file(directory, stage_id))
    return {int(ordinal): tuple(branches or ())
            for ordinal, branches in zip(table.column(_ROW_KEY).to_pylist(),
                                         table.column(_BRANCHES_KEY).to_pylist())}


def _write_options(path: Path, options: Mapping[BranchId, BranchOption]) -> None:
    held = list(options.values())
    write_frame_table(pa.table({name: [getattr(option, name) for option in held]
                                for name in _OPTION_FIELDS}), path)


def _read_options(path: Path) -> dict[BranchId, BranchOption]:
    table = read_frame_table(path)
    cells = {name: table.column(name).to_pylist() for name in _OPTION_FIELDS}
    options = [BranchOption(**{name: cells[name][row] for name in _OPTION_FIELDS})
               for row in range(table.num_rows)]
    return {option.id: option for option in options}


def _write_row_counts(path: Path, counted: Mapping[BranchId, int]) -> None:
    # Its own file: a branch whose every row was removed is counted and never offered.
    write_frame_table(pa.table({_BRANCH_KEY: list(counted),
                                _COUNT_KEY: list(counted.values())},
                               schema=_ROW_COUNTS_SCHEMA), path)


def _read_row_counts(path: Path) -> Counter[BranchId]:
    table = read_frame_table(path)
    return Counter(dict(zip(table.column(_BRANCH_KEY).to_pylist(),
                            table.column(_COUNT_KEY).to_pylist())))


def _write_stamp(path: Path, stamp: BranchCacheStamp) -> None:
    # Keeps the .parquet suffix: write_frame_table picks its format off it.
    beside = path.with_suffix(".part.parquet")
    write_frame_table(
        pa.table({_VERSION_KEY: [stamp.pinned_version_id],
                  _STAGE_KEY: [stamp.list_stage_ids()],
                  _COUNT_KEY: [[size.row_count for size in stamp.frame_sizes]]},
                 schema=_STAMP_SCHEMA),
        beside)
    # Renamed into place so a reader never meets a stamp mid-write.
    beside.replace(path)


def _read_stamp(path: Path) -> BranchCacheStamp | None:
    if not path.exists():
        return None
    try:
        table = read_frame_table(path)
    except (pa.ArrowInvalid, OSError):
        return None
    return BranchCacheStamp(
        pinned_version_id=table.column(_VERSION_KEY)[0].as_py(),
        frame_sizes=[StageFrameSize(stage_id=stage_id, row_count=row_count)
                     for stage_id, row_count in zip(table.column(_STAGE_KEY)[0].as_py(),
                                                    table.column(_COUNT_KEY)[0].as_py())])


def _resolve_cache_dir(run_dir: Path) -> Path:
    return Path(run_dir) / _CACHE_DIR


def _resolve_paths_file(directory: Path, stage_id: StageId) -> Path:
    return directory / f"{stage_id}.paths.parquet"


def _resolve_merges_file(directory: Path, stage_id: StageId) -> Path:
    return directory / f"{stage_id}.merges.parquet"

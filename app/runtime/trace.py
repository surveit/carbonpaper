"""Walk one row of a run back to its source, off the run directory alone."""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import pyarrow as pa

from app.core.errors import ContributorNotInFanIn, RowOutOfRange, StageNotInRun
from app.core.frames import convert_row_to_json_cells, read_frame_table
from app.models.stage import StageType, is_grain_and_order_preserving
from app.runtime.lineage import EdgeKind, RowLineage, RowParent
from app.runtime.lineage_sidecar import read_lineage_sidecar
from app.models.run_manifest import read_input_bindings
from app.runtime.manifest import read_run_manifest, resolve_output_path


def _is_row_preserving(stage_type: str) -> bool:
    try:
        return is_grain_and_order_preserving(StageType(stage_type))
    except ValueError:
        return False


@dataclass(frozen=True)
class RowSampleChoice:
    """Which row a caller wants sampled at the fan-in whose rows live in `stage_id`."""

    stage_id: str
    row_ordinal: int


@dataclass(frozen=True)
class RowSample:
    """Where the sampled row stood: 1-based `place` within `of`, in row order."""

    place: int
    of: int


@dataclass
class StageTransform:
    stage_id: str
    stage_type: str
    row_ordinal: int
    row: dict[str, Any]     # the row's cells, verbatim
    columns_new: list[str]  # columns first appearing at this stage vs its parent
    origin: str             # "source" | "computed" | "llm" | "other"
    # Recorded parents of this row that the walk did NOT follow — the other side
    # of a join, and later an aggregate's contributors. Each is a starting point
    # for a trace of its own, which is how the reader promotes a branch onto the
    # spine; the walk itself stays a single chain (see `Trace.steps`).
    branches: list[RowParent] = field(default_factory=list)
    # `row_ordinal` counts across the concatenation; `source_row` counts within the file.
    source_file: str | None = None
    source_row: int | None = None
    # How many files the stage read; None where the manifest did not record any binding.
    source_file_count: int | None = None
    # Set where the walk sampled one of the rows summarized into this one.
    sampled: RowSample | None = None


@dataclass
class TraceEnd:
    reached_origin: bool
    at_stage: str
    message: str


@dataclass
class Trace:
    run_id: str
    start_stage: str
    start_row: int
    steps: list[StageTransform]  # newest first: start stage, then each ancestor
    end: TraceEnd


def _load_manifest(run_dir: Path) -> dict[str, Any]:
    """The run's recorded manifest, found by the (project, run id) its dir names."""
    return read_run_manifest(run_dir.parent.parent.name, run_dir.name).to_dict()


def _count_files_read(manifest: dict[str, Any]) -> Counter[str]:
    return Counter(binding.stage_id for binding in read_input_bindings(manifest))


def _stages_by_id(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["stage_id"]: s for s in manifest.get("stage_records", [])}


def _parents(stage_record: dict[str, Any]) -> list[str]:
    parents: list[str] = []
    for entry in stage_record.get("input_validation_report") or []:
        phase = entry.get("phase", "")
        if phase.startswith("input:"):
            parents.append(phase.split(":", 1)[1])
    return parents


def _origin(stage_type: str) -> str:
    return {
        "input_data": "source",
        "python_row_function": "computed",
        "starlark_row_function": "computed",
        "llm_transform": "llm",
    }.get(stage_type, "other")


def _read_output(run_dir: Path, stage_record: dict[str, Any]) -> pa.Table | None:
    path = resolve_output_path(Path(run_dir), stage_record.get("output_path"))
    if path is None or not path.exists():
        return None
    return read_frame_table(path)


def _scalar(value: Any) -> Any:
    if hasattr(value, "tolist") and not isinstance(value, str):
        # Plain Python, so the row is JSON-able.
        value = value.tolist()
    # A pandas-null numeric cell (NaN, +-inf) becomes None: JSON has no
    # non-finite float token, and 0 would misrepresent a value the source
    # never reported.
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _row_dict(table: pa.Table, r: int) -> dict[str, Any]:
    return {name: _scalar(table.column(name)[r].as_py()) for name in table.column_names}


def _split_spine(hops: list[RowParent]) -> tuple[RowParent | None, list[RowParent]]:
    spine = next((p for p in hops if p.kind == EdgeKind.direct.value), None)
    return spine, [p for p in hops if p is not spine]


def _new_columns(child: pa.Table, parent: pa.Table | None) -> list[str]:
    if parent is None:
        return list(child.column_names)
    parent_cols = set(parent.column_names)
    return [c for c in child.column_names if c not in parent_cols]


def _not_preserving_message(stage_type: str) -> str:
    return (f"stops at {stage_type} — it reshapes rows, so row-level lineage "
            "across it needs recording (issue #58)")


def _columns_parent_id(parents: list[str], spine: RowParent | None) -> str | None:
    """With no spine and 2+ inputs there is no parent to diff, so every column reads as new."""
    if spine is not None:
        return spine.stage_id
    return parents[0] if len(parents) == 1 else None


def _advance_via_lineage(
    frames: RunFrames, by_id: dict[str, dict[str, Any]], sid: str, spine: RowParent,
) -> tuple[str, int] | TraceEnd:
    if spine.stage_id not in by_id:
        return TraceEnd(False, sid, "the parent named in lineage is not in the run")
    parent_output = frames.output(by_id[spine.stage_id])
    if parent_output is None:
        return TraceEnd(False, spine.stage_id, "this stage's output file is missing from the run")
    if spine.row_ordinal < 0 or spine.row_ordinal >= parent_output.num_rows:
        return TraceEnd(False, sid, "lineage names an out-of-range parent row")
    return spine.stage_id, spine.row_ordinal


def _advance_positionally(
    frames: RunFrames, by_id: dict[str, dict[str, Any]], sid: str,
    parent_id: str, r: int, table: pa.Table,
) -> tuple[str, int] | TraceEnd:
    if parent_id not in by_id:
        return TraceEnd(False, sid, "the parent named in the manifest is not in the run")
    parent_table = frames.output(by_id[parent_id])
    if parent_table is None:
        return TraceEnd(False, parent_id, "this stage's output file is missing from the run")
    if parent_table.num_rows != table.num_rows:
        return TraceEnd(False, sid,
                         "this stage's row count differs from its input, so per-row "
                         "position can't be trusted (issue #58)")
    return parent_id, r


def _summarizes_nothing_message() -> str:
    return ("this row summarizes its inputs, and the run recorded that no input "
            "row fed it — an aggregation over an empty group")


def _find_fan_in(stage_type: str, spine: RowParent | None,
                hops: list[RowParent] | None) -> list[RowParent] | None:
    """The rows summarized into this one, in row order; None where the walk faces no fan-in."""
    if stage_type == StageType.input_data or spine is not None or hops is None:
        return None
    return sorted((p for p in hops if p.kind == EdgeKind.contribution.value),
                  key=lambda p: (p.stage_id, p.row_ordinal))


def _sample_from_fan_in(sid: str, fan_in: list[RowParent],
                        pending: list[RowSampleChoice]) -> RowParent | None:
    """The lowest-numbered row of the fan-in, unless the caller named one here."""
    if not fan_in:
        return None
    if pending and pending[0].stage_id in {p.stage_id for p in fan_in}:
        return _match_named_contributor(sid, fan_in, pending.pop(0))
    return fan_in[0]


def _match_named_contributor(sid: str, fan_in: list[RowParent],
                             choice: RowSampleChoice) -> RowParent:
    for parent in fan_in:
        if (parent.stage_id, parent.row_ordinal) == (choice.stage_id, choice.row_ordinal):
            return parent
    raise ContributorNotInFanIn(
        f"{choice.stage_id!r} row {choice.row_ordinal} is not one of the "
        f"{len(fan_in)} rows the run recorded as feeding {sid!r}"
    )


def _refuse_a_choice_the_walk_never_met(pending: list[RowSampleChoice]) -> None:
    if not pending:
        return
    left = pending[0]
    raise ContributorNotInFanIn(
        f"this walk met no fan-in over {left.stage_id!r} to follow its row "
        f"{left.row_ordinal} at"
    )


def _read_row_sample(fan_in: list[RowParent] | None,
                     sampled: RowParent | None) -> RowSample | None:
    if fan_in is None or sampled is None:
        return None
    return RowSample(fan_in.index(sampled) + 1, len(fan_in))


def _advance(
    frames: RunFrames, by_id: dict[str, dict[str, Any]], sid: str, stage_type: str, r: int,
    table: pa.Table, parents: list[str], spine: RowParent | None,
    fan_in: list[RowParent] | None = None, followed: RowParent | None = None,
) -> tuple[str, int] | TraceEnd:
    if stage_type == StageType.input_data:
        return TraceEnd(True, sid, "input_data stage — the rows originate here")
    if spine is not None:
        return _advance_via_lineage(frames, by_id, sid, spine)
    if followed is not None:
        return _advance_via_lineage(frames, by_id, sid, followed)
    if fan_in is not None:
        return TraceEnd(False, sid, _summarizes_nothing_message())
    if not parents:
        return TraceEnd(False, sid, "the manifest records no input edge for this stage")
    # Nothing recorded: the only remaining route is the ordinal, and only where
    # the type guarantees it. A join is NOT crossable this way even though an
    # enrich's output happens to be in subject order — a run made before this
    # recorded lineage stops here, and re-running it is what makes it traceable.
    if not _is_row_preserving(stage_type) or len(parents) != 1:
        return TraceEnd(False, sid, _not_preserving_message(stage_type))
    return _advance_positionally(frames, by_id, sid, parents[0], r, table)


class RunFrames:
    """One run's outputs and parsed lineage sidecars, each read at most once."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self._outputs: dict[str, pa.Table | None] = {}
        self._sidecars: dict[str, RowLineage | None] = {}

    def output(self, stage_record: dict[str, Any]) -> pa.Table | None:
        stage_id = str(stage_record.get("stage_id", ""))
        if stage_id not in self._outputs:
            self._outputs[stage_id] = _read_output(self.run_dir, stage_record)
        return self._outputs[stage_id]

    def lineage_hops(self, stage_id: str, row_ordinal: int) -> list[RowParent] | None:
        """None and [] differ: [] is a recorded no-parents fact; None means none was recorded."""
        lineage = self._sidecar(stage_id)
        if lineage is None or not 0 <= row_ordinal < len(lineage):
            return None
        return list(lineage.parents[row_ordinal])

    def _sidecar(self, stage_id: str) -> RowLineage | None:
        if stage_id not in self._sidecars:
            # Parsing a 45k-row sidecar to read one row is the whole cost of a
            # trace, so a run traced row by row must not pay it per row.
            self._sidecars[stage_id] = read_lineage_sidecar(self.run_dir, stage_id).lineage
        return self._sidecars[stage_id]


def trace_row(run_dir: Path, stage_id: str, row_ordinal: int,
              follow: Sequence[RowSampleChoice] = ()) -> Trace:
    """Raises only for caller errors (bad stage id, ordinal or choice); else a TraceEnd."""
    return trace_row_from(RunFrames(run_dir), stage_id, row_ordinal, follow)


def trace_row_from(frames: RunFrames, stage_id: str, row_ordinal: int,
                   follow: Sequence[RowSampleChoice] = ()) -> Trace:
    """As `trace_row`, over a reader many rows of one run share."""
    run_dir = frames.run_dir
    manifest = _load_manifest(run_dir)
    by_id = _stages_by_id(manifest)
    files_read = _count_files_read(manifest)
    if stage_id not in by_id:
        raise StageNotInRun(f"stage {stage_id!r} not in run {run_dir.name}")

    steps: list[StageTransform] = []
    sid, r = stage_id, row_ordinal
    end: TraceEnd | None = None
    pending = list(follow)

    while end is None:
        record = by_id[sid]
        stage_type = record.get("type", "")
        table = frames.output(record)
        if table is None:
            end = TraceEnd(False, sid, "this stage's output file is missing from the run")
            break
        if r < 0 or r >= table.num_rows:
            raise RowOutOfRange(
                f"row {r} out of range for stage {sid!r} ({table.num_rows} rows)"
            )

        parents = _parents(record)
        hops = frames.lineage_hops(sid, r)
        spine, branches = _split_spine(hops or [])
        fan_in = _find_fan_in(stage_type, spine, hops)
        followed = None if fan_in is None else _sample_from_fan_in(sid, fan_in, pending)
        columns_parent_id = _columns_parent_id(parents, spine)
        parent_table = (
            frames.output(by_id[columns_parent_id])
            if columns_parent_id in by_id else None
        )

        steps.append(StageTransform(
            stage_id=sid,
            stage_type=stage_type,
            row_ordinal=r,
            row=_row_dict(table, r),
            columns_new=_new_columns(table, parent_table),
            origin=_origin(stage_type),
            branches=branches,
            source_file=spine.source_file if spine else None,
            source_row=spine.row_ordinal if spine and spine.source_file else None,
            source_file_count=files_read.get(sid),
            sampled=_read_row_sample(fan_in, followed),
        ))

        next_hop = _advance(
            frames, by_id, sid, stage_type, r, table, parents, spine, fan_in, followed
        )
        if isinstance(next_hop, TraceEnd):
            end = next_hop
        else:
            sid, r = next_hop

    _refuse_a_choice_the_walk_never_met(pending)
    return Trace(
        run_id=manifest.get("run_id", run_dir.name),
        start_stage=stage_id,
        start_row=row_ordinal,
        steps=steps,
        end=end,
    )


def trace_to_dict(trace: Trace) -> dict[str, Any]:
    return {
        "run_id": trace.run_id,
        "start_stage": trace.start_stage,
        "start_row": trace.start_row,
        "steps": [
            {
                "stage_id": step.stage_id,
                "stage_type": step.stage_type,
                "row_ordinal": step.row_ordinal,
                "row": convert_row_to_json_cells(step.row),
                "columns_new": step.columns_new,
                "origin": step.origin,
                "source_file": step.source_file,
                "source_row": step.source_row,
                "source_file_count": step.source_file_count,
                "sampled": None if step.sampled is None else {
                    "place": step.sampled.place,
                    "of": step.sampled.of,
                },
                "branches": [
                    {
                        "stage_id": branch.stage_id,
                        "row_ordinal": branch.row_ordinal,
                        "kind": str(branch.kind),
                        "columns": None if branch.columns is None else list(branch.columns),
                    }
                    for branch in step.branches
                ],
            }
            for step in trace.steps
        ],
        "end": {
            "reached_origin": trace.end.reached_origin,
            "at_stage": trace.end.at_stage,
            "message": trace.end.message,
        },
    }

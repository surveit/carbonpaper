"""What a reader is handed to check that a figure covered the right rows.

One `ScopeMap` answers one citation. See docs/scope-map.md.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from app.models.claims import StageOutputCellCitation

# A stage's id within one workflow version.
StageId = str
# f"{stage_id}|{branch_id}", unique across a run. NOT a join key or a group-by
# column, both of which this codebase already calls a key.
BranchId = str
# A row's position in its stage's output frame.
RowOrdinal = int
# The branches one row passed through, ordered by stage position then source line.
BranchPath = tuple[BranchId, ...]
# A frame cell. The workflow declares its own columns, so the shape is unknown here.
ScalarCell = str | int | float | bool | None
# One row's own columns, keyed by column name. Unchecked: see ScalarCell.
RowCells = dict[str, ScalarCell]


class BranchOrigin(StrEnum):
    """Why the rows on either side of a branch differ. Names avoid `str` methods."""

    code = "code"            # an if/elif/else/try/except the row ran
    predicate = "predicate"  # a filter kept it or dropped it
    lookup = "lookup"        # a reference input matched it or missed it
    union = "union"          # which input of a union it arrived on
    load = "load"            # the stage that read it off disk
    aggregate = "aggregate"  # which group of an aggregate it fed


class BranchRole(StrEnum):
    """What became of the rows not in this figure. See docs/scope-map.md."""

    removes = "removes"    # taken out of the frame: a filter's drop, a dedupe's loser
    excludes = "excludes"  # left in the frame, but in another group of an aggregate
    arm = "arm"            # neither — the rows are still downstream


class BranchFact(BaseModel):
    """One distinction the run recorded, and where to look at the code behind it."""

    id: BranchId
    stage: StageId
    origin: BranchOrigin
    role: BranchRole
    label: str
    # The code the branch decided in: the arm's own line, a filter's source, a join's
    # key pairs. Empty where the stage declares the decision rather than writing it.
    source: str = ""
    # The line the arm's test is on, and the line its body starts. None where there
    # is no code to point at, which is every origin but `code`.
    tested_at: int | None = None
    decided_at: int | None = None


class BranchingStage(BaseModel):
    id: StageId
    type: str
    description: str
    # Index in the run's execution order: the map's left-to-right.
    position: int
    code: str = ""


class FrameScale(BaseModel):
    """How much of one frame the figure descends from. A count, never a ribbon."""

    stage: StageId
    rows: int
    covered: int
    # A join's second input is a lookup table, not part of the flow being narrowed.
    reference: bool = False


class ContributingRow(BaseModel):
    ordinal: RowOrdinal
    # Index into `ScopeMap.paths`.
    path: int
    cells: RowCells
    # What this row put into the figure — the aggregation's `value_column` cell. Set
    # only where the figure's formula read this very frame; see `ContributingRowSet`.
    contribution: ScalarCell = None


class ContributingRowSet(BaseModel):
    """The rows the cited cell was computed from, at one stage's grain."""

    at_stage: StageId
    ordinals: list[RowOrdinal]
    # The aggregates walked down through, nearest the cited cell first.
    stages_traced_through: list[StageId] = []
    # These rows always answer WHICH rows reached the figure; their values total to
    # it only when this frame is the one the formula read and the formula adds.
    adds_up: bool = False
    # Set when `rows` was sampled, so a reader never mistakes a sample for the whole.
    sampled_from: int | None = None


class CutRows(BaseModel):
    """The rows behind one branch this figure's rows did not take."""

    branch: BranchId
    at_stage: StageId
    # Counts cover every row; `rows` is a sample. See docs/scope-map.md.
    total: int
    paths: list[BranchPath]
    # Parallel to `paths`: how many rows took each one.
    path_rows: list[int]
    rows: list[ContributingRow]
    stages: list[BranchingStage]


class BranchReach(BaseModel):
    """`taken` counts every row of the run; `here` only this figure's."""

    branch: BranchId
    taken: int
    here: int


class ScopeMap(BaseModel):
    """Which rows produced one cited figure, and what told them apart from the rest."""

    project_id: str
    run: str
    citation: StageOutputCellCitation
    formula: str | None
    value_column: str | None
    covers: ContributingRowSet
    rows: list[ContributingRow]
    columns: list[str]
    # Distinct paths among `covers`, and the path each covered row is on.
    paths: list[BranchPath]
    path_index: list[int]
    branches: dict[BranchId, BranchFact]
    stages: list[BranchingStage]
    reach: list[BranchReach]
    scale: list[FrameScale]

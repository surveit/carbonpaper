"""What told one row apart from another. See docs/branch-analysis.md."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from app.models.schema import StageId

# f"{stage_id}|{branch_id}", unique in a run. Not a join or group-by key.
BranchId = str
# A row's position in its stage's output frame.
RowOrdinal = int
# The branches one row passed through, ordered by stage position then source line.
BranchPath = tuple[BranchId, ...]


class BranchReason(StrEnum):
    """Why the rows on either side of a branch differ. See docs/branch-analysis.md."""

    code = "code"
    predicate = "predicate"
    join = "join"  # type: ignore[assignment]  # shadows str.join; never called
    union = "union"
    load = "load"
    aggregate = "aggregate"


class BranchRole(StrEnum):
    """What the branch did to the rows that took it."""

    removes = "removes"    # taken out of the frame
    excludes = "excludes"  # in another group
    keeps = "keeps"        # still in the frame, and still downstream


class BranchFact(BaseModel):
    """One distinction the run recorded, and where to look at the code behind it."""

    id: BranchId
    stage: StageId
    reason: BranchReason
    role: BranchRole
    label: str
    # The code the branch decided in. Empty where the stage declares it instead.
    source: str = ""
    # The branch's test line and its body's first line. None for every reason but code.
    tested_at: int | None = None
    decided_at: int | None = None


class RowSet(BaseModel):
    """Rows at one stage's grain: what a figure was computed from, or what a branch cut."""

    at_stage: StageId
    ordinals: list[RowOrdinal]
    # The aggregates walked down through, nearest the cited cell first.
    aggregates_walked_down: list[StageId] = []


class FrameScale(BaseModel):
    """One stage's frame size beside how much of it a figure came through."""

    stage: StageId
    rows_count: int
    included_rows_count: int
    # A join's second input is a lookup table: its size is not the flow narrowing.
    is_a_lookup_table: bool = False

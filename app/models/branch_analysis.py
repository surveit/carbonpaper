"""What told one row apart from another. See docs/branch-analysis.md."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from app.models.schema import StageId

# Opaque, and unique in a run. Nothing reads anything back out of it.
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
    merge = "merge"


class BranchRole(StrEnum):
    """What the branch did to the rows that took it."""

    removes = "removes"  # taken out of the frame
    keeps = "keeps"      # still in the frame, and still downstream


class BranchOption(BaseModel):
    """One option a stage offered its rows. How many it had can be data-decided."""

    id: BranchId
    # The stage that made the decision.
    stage_id: StageId
    # The frame this branch's rows are rows of. See docs/branch-analysis.md.
    rows_live_in_stage_id: StageId
    reason: BranchReason
    role: BranchRole
    # Empty for a merge: its words name a row, which the reader's layer writes.
    label: str = ""
    source_code: str = ""
    # None for every reason but `code`, which is the only one written in a stage's source.
    test_line_number: int | None = None
    first_body_line_number: int | None = None
    last_body_line_number: int | None = None
    # The data-decided part. See docs/branch-analysis.md.
    merged_into_row_ordinal: RowOrdinal | None = None


class RowSet(BaseModel):
    """Rows at one stage's grain: what a figure was computed from, or what a branch cut."""

    at_stage: StageId
    ordinals: list[RowOrdinal]
    # Every stage that re-grained on the way down, nearest the cited cell first.
    regrained_at: list[StageId] = []
    # The ordinals above that no input row fed.
    fed_by_no_rows: list[RowOrdinal] = []


class FrameScale(BaseModel):
    """One stage's frame size beside how much of it a figure came through."""

    stage: StageId
    rows_count: int
    included_rows_count: int

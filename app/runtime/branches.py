"""Which branch of its code each output row took."""
from __future__ import annotations

from dataclasses import dataclass, field

import pyarrow as pa

from .errors import BranchRecordingError

BRANCHES_KEY = "_branches"

# Pinned: left to infer, an empty sidecar types its column `null`.
BRANCH_SCHEMA = pa.schema([(BRANCHES_KEY, pa.list_(pa.string()))])

# None where the run never executed that row; taking no branch is a different thing.
BranchesTaken = tuple[str, ...] | None


@dataclass(frozen=True)
class RowBranches:
    """Entry i belongs to output row i."""

    taken: list[BranchesTaken] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.taken)

    def to_table(self) -> pa.Table:
        return pa.table(
            {BRANCHES_KEY: [None if ids is None else list(ids) for ids in self.taken]},
            schema=BRANCH_SCHEMA,
        )

    @classmethod
    def from_table(cls, table: pa.Table) -> "RowBranches":
        cells = table.column(BRANCHES_KEY).to_pylist()
        return cls([None if cell is None else tuple(cell) for cell in cells])


class BranchRecorder:
    """One per stage execution. A row must be open to record into, and open exactly once."""

    def __init__(self) -> None:
        self._open_row: int | None = None
        self._taken: list[str] = []
        self._by_input: dict[int, tuple[str, ...]] = {}

    def open_row(self, index: int) -> None:
        if self._open_row is not None:
            raise BranchRecordingError(f"row {self._open_row} was never closed")
        self._open_row = index
        self._taken.clear()

    def record(self, branch_id: str) -> None:
        if self._open_row is None:
            raise BranchRecordingError(f"branch {branch_id!r} reported outside a row")
        self._taken.append(branch_id)

    def close_row(self) -> None:
        if self._open_row is None:
            raise BranchRecordingError("no row is open")
        self._by_input[self._open_row] = tuple(self._taken)
        self._open_row = None
        self._taken.clear()

    def branches_for(self, index: int) -> BranchesTaken:
        return self._by_input.get(index)

    def rows_kept(self, kept_indices: list[int]) -> RowBranches | None:
        """Reindexed onto OUTPUT rows. None where the code never branched at all."""
        if not any(self._by_input.values()):
            return None
        return RowBranches([self._by_input.get(index) for index in kept_indices])

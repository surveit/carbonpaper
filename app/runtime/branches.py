"""Which branch of its code each output row took."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pyarrow as pa

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
    """One per stage execution, keyed by INPUT ordinal so an unrun row is absent, not empty."""

    def __init__(self) -> None:
        self._taken: list[str] = []
        self._by_input: dict[int, tuple[str, ...]] = {}

    def record(self, branch_id: str) -> None:
        self._taken.append(branch_id)

    def close_row(self, index: int) -> None:
        self._by_input[index] = tuple(self._taken)
        self._taken.clear()

    def branches_for(self, index: int) -> BranchesTaken:
        return self._by_input.get(index)


def branch_sidecar_path(run_dir: Path, stage_id: str) -> Path:
    return Path(run_dir) / "outputs" / f"{stage_id}.branch.parquet"

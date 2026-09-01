"""One traced step's row, a line per column, marked against the parent it came from."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.web.column_order import (
    ColumnGroup,
    find_column_group,
    order_columns_by_group,
)
from app.web.diff_state import CellDiffState


@dataclass(frozen=True)
class RowColumn:
    name: str
    state: CellDiffState
    text: str
    # Only a changed cell carries one; a dropped cell puts the discarded parent
    # value in `text`, because no output value exists to show beside it.
    was: Optional[str]
    # The stage's signature says the transform consumes it.
    read: bool = False

    @property
    def group(self) -> ColumnGroup:
        return find_column_group(self.state.value, read=self.read,
                                 changed=self.state is CellDiffState.changed)

    @property
    def inert(self) -> bool:
        """Nothing happened to it here, and nothing here read it."""
        return self.group is ColumnGroup.untouched


@dataclass(frozen=True)
class RowDiff:
    columns: list[RowColumn]
    added: int
    changed: int
    dropped: int


def build_row_diff(
    row: dict[str, Any], parent_row: Optional[dict[str, Any]], *, is_origin: bool,
    read: frozenset[str] = frozenset(),
) -> RowDiff:
    if parent_row is None:
        state = CellDiffState.added if is_origin else CellDiffState.carried
        return _count_columns([
            RowColumn(name=str(name), state=state, text=render_cell(value), was=None,
                      read=str(name) in read)
            for name, value in row.items()
        ])
    return _count_columns(
        [_compare_column(str(name), value, parent_row, read) for name, value in row.items()]
        + [
            RowColumn(name=str(name), state=CellDiffState.dropped,
                     text=render_cell(value), was=None)
            for name, value in parent_row.items()
            if str(name) not in row
        ]
    )


def row_diff_to_dict(diff: RowDiff) -> dict[str, Any]:
    return {
        "columns": [
            {"name": column.name, "state": str(column.state.value),
             "text": column.text, "was": column.was, "inert": column.inert}
            for column in diff.columns
        ],
        "added": diff.added,
        "changed": diff.changed,
        "dropped": diff.dropped,
    }


def render_cell(value: Any) -> str:
    return "" if value is None else str(value)


def _compare_column(
    name: str, value: Any, parent_row: dict[str, Any], read: frozenset[str]
) -> RowColumn:
    """Compared as RENDERED text: a difference nobody can see is not marked."""
    text = render_cell(value)
    if name not in parent_row:
        return RowColumn(name=name, state=CellDiffState.added, text=text, was=None)
    was = render_cell(parent_row[name])
    if was == text:
        return RowColumn(name=name, state=CellDiffState.carried, text=text, was=None,
                         read=name in read)
    return RowColumn(name=name, state=CellDiffState.changed, text=text, was=was)


def _count_columns(columns: list[RowColumn]) -> RowDiff:
    columns = order_columns_by_group(columns)

    def count(state: CellDiffState) -> int:
        return sum(1 for column in columns if column.state is state)

    return RowDiff(
        columns=columns,
        added=count(CellDiffState.added),
        changed=count(CellDiffState.changed),
        dropped=count(CellDiffState.dropped),
    )

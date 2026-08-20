"""One traced step's row as a list of fields, each marked against the parent row
the walk came from — the transposed single-row form the lineage page reads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.web.stage_diff import CellDiffState


@dataclass(frozen=True)
class RowField:
    name: str
    state: CellDiffState
    text: str
    # Only a changed cell carries one; a dropped cell puts the discarded parent
    # value in `text`, because no output value exists to show beside it.
    was: Optional[str]


@dataclass(frozen=True)
class RowDiff:
    fields: list[RowField]
    added: int
    changed: int
    dropped: int


def build_row_diff(
    row: dict[str, Any], parent_row: Optional[dict[str, Any]], *, is_origin: bool
) -> RowDiff:
    if parent_row is None:
        state = CellDiffState.added if is_origin else CellDiffState.carried
        return _count_fields([
            RowField(name=str(name), state=state, text=_render(value), was=None)
            for name, value in row.items()
        ])
    return _count_fields(
        [_compare_field(str(name), value, parent_row) for name, value in row.items()]
        + [
            RowField(name=str(name), state=CellDiffState.dropped,
                     text=_render(value), was=None)
            for name, value in parent_row.items()
            if str(name) not in row
        ]
    )


def row_diff_to_dict(diff: RowDiff) -> dict[str, Any]:
    return {
        "fields": [
            {"name": field.name, "state": str(field.state.value),
             "text": field.text, "was": field.was}
            for field in diff.fields
        ],
        "added": diff.added,
        "changed": diff.changed,
        "dropped": diff.dropped,
    }


def _compare_field(name: str, value: Any, parent_row: dict[str, Any]) -> RowField:
    """Compared as RENDERED text: a difference nobody can see is not marked."""
    text = _render(value)
    if name not in parent_row:
        return RowField(name=name, state=CellDiffState.added, text=text, was=None)
    was = _render(parent_row[name])
    if was == text:
        return RowField(name=name, state=CellDiffState.carried, text=text, was=None)
    return RowField(name=name, state=CellDiffState.changed, text=text, was=was)


def _count_fields(fields: list[RowField]) -> RowDiff:
    def count(state: CellDiffState) -> int:
        return sum(1 for field in fields if field.state is state)

    return RowDiff(
        fields=fields,
        added=count(CellDiffState.added),
        changed=count(CellDiffState.changed),
        dropped=count(CellDiffState.dropped),
    )


def _render(value: Any) -> str:
    return "" if value is None else str(value)

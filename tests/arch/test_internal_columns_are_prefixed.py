"""Architecture: every internal row column the row driver attaches sits under
`INTERNAL_COLUMN_PREFIX` — the namespace a stage is forbidden to declare (see
app.models.stages.shared), which buys collision-freedom for columns inside it and
for nothing else. `_INTERNAL_ROW_COLUMNS` is the ONE declaration of those columns,
so reading it is the whole rule, and it going missing is a failure of its own.
"""
from __future__ import annotations

from app.models.stages.shared import INTERNAL_COLUMN_PREFIX
from app.runtime.stages import execution

_TABLE = "_INTERNAL_ROW_COLUMNS"


def read_declaration_table() -> tuple:
    table = getattr(execution, _TABLE, None)
    assert table, (
        f"app/runtime/stages/execution.py no longer declares a non-empty {_TABLE} — "
        "that table is the single declaration of the row driver's internal columns "
        "and the only thing this rule reads. Point it at the new declaration (and "
        "keep that one exhaustive), or the prefix invariant goes unchecked."
    )
    return table


def test_every_internal_row_column_sits_under_the_reserved_prefix() -> None:
    offenders = [
        entry.column
        for entry in read_declaration_table()
        if not entry.column.startswith(INTERNAL_COLUMN_PREFIX)
    ]
    assert not offenders, (
        f"an internal row column must be named with the `{INTERNAL_COLUMN_PREFIX}` "
        "prefix a stage is forbidden to declare — outside it, the column can collide "
        f"with a real one the compiler authored and the stage-side ban buys it "
        f"nothing: {offenders}"
    )


def test_the_declaration_table_is_present_and_non_empty() -> None:
    assert len(read_declaration_table()) >= 1

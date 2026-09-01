"""Order for a stage's table, from the signature alone: read, rewritten, added, rest."""
from __future__ import annotations

from app.models import WorkflowStage
from app.models.stage import parse_stage
from app.models.stages.signature import list_written_column_names
from app.web.column_order import order_columns_by_signature, order_preview_columns
from conftest import place_stage, reads_of

LOAD_ID = "load"

_IN_COLUMNS = [
    {"name": "name", "type": "str", "nullable": True},
    {"name": "val", "type": "int", "nullable": True},
    {"name": "junk", "type": "str", "nullable": True},
]


def _extends_stage() -> WorkflowStage:
    return place_stage(parse_stage({
        "id": "classify", "description": "Classify", "type": "python_row_function",
        "inputs": [{"id": LOAD_ID}],
        "function": {"kind": "inline",
                     "code": "def transform(row):\n    return row\n"},
        "signature": {
            "form": "extends",
            "reads": reads_of(LOAD_ID, _IN_COLUMNS),
            "rewrites": [{"name": "name", "type": "str", "nullable": True}],
            "adds": [{"name": "label", "type": "str", "nullable": True}],
        },
    }))


def _replaces_stage() -> WorkflowStage:
    return place_stage(parse_stage({
        "id": "roll_up", "description": "Roll up", "type": "aggregate",
        "inputs": [{"id": LOAD_ID}],
        "aggregate": {"group_by": ["name"],
                      "aggregations": [{"output_column": "total", "formula": "sum",
                                        "value_column": "val"}]},
        "signature": {
            "form": "replaces",
            "reads": reads_of(LOAD_ID, _IN_COLUMNS),
            "produces": [{"name": "total", "type": "int", "nullable": True},
                         {"name": "name", "type": "str", "nullable": True}],
        },
    }))


def test_what_the_stage_read_leads_then_the_rewrite_then_the_addition() -> None:
    # classify reads all three, rewrites `name` and adds `label`.
    ordered = order_columns_by_signature(
        _extends_stage(), ["name", "val", "junk", "label"])

    assert ordered == ["val", "junk", "name", "label"]


def test_a_replaces_stage_keeps_frame_order() -> None:
    # Nothing flows through it, so no subset of its output is the stage's own work.
    ordered = order_columns_by_signature(_replaces_stage(), ["name", "total"])

    assert ordered == ["name", "total"]
    assert list_written_column_names(_replaces_stage().stage) == []


def test_an_unresolvable_stage_definition_keeps_frame_order() -> None:
    assert order_columns_by_signature(None, ["name", "val"]) == ["name", "val"]


def test_a_declared_column_the_frame_does_not_carry_is_not_invented() -> None:
    ordered = order_columns_by_signature(_extends_stage(), ["name", "val"])

    assert ordered == ["val", "name"]


def test_the_preview_table_is_reordered_without_touching_its_rows() -> None:
    rows = [{"name": "a", "val": 1, "junk": "x", "label": "big"}]
    table = {"columns": ["name", "val", "junk", "label"], "preview": rows,
             "rows_total": 1}

    ordered = order_preview_columns(table, _extends_stage())

    assert ordered is not None
    assert ordered["columns"] == ["val", "junk", "name", "label"]
    assert ordered["preview"] == rows
    assert table["columns"] == ["name", "val", "junk", "label"]


def test_a_preview_that_failed_to_load_is_passed_through() -> None:
    assert order_preview_columns(None, _extends_stage()) is None
    assert order_preview_columns({"error": "missing on disk"}, _extends_stage()) == {
        "error": "missing on disk"}

from __future__ import annotations

import pyarrow as pa
import pytest

from app.models import parse_stage, Stage
from app.models.run_manifest import StageContribution
from app.runtime.stages.execution import _trim_to_declared_columns
from conftest import place_stage


def _rating_stage() -> Stage:
    """The stage id is distinct from every declared column, so `"rate" in message` cannot false-match."""
    return parse_stage({
        "id": "rate", "description": "Rate", "type": "python_row_function",
        "inputs": [{"id": "load"}],
        "signature": {
            "form": "extends",
            "reads": [
                {
                    "input": "load",
                    "columns": [{"name": "id", "type": "str", "nullable": True}],
                },
            ],
            "adds": [
                {"name": "score", "type": "int", "nullable": True},
                {"name": "verdict", "type": "str", "nullable": True},
            ],
        },
        "function": {"kind": "inline", "code": "def transform(row): return row"},
    })


def test_projection_raises_naming_the_stage_and_every_missing_column():
    table = pa.table({"id": ["r1"], "leftover": [1]})

    with pytest.raises(ValueError) as excinfo:
        _trim_to_declared_columns(place_stage(_rating_stage()), table, StageContribution())

    message = str(excinfo.value)
    assert "rate" in message
    assert "score" in message and "verdict" in message   # every missing one, not just the first


def test_projection_keeps_declared_order_and_reports_what_it_dropped():
    table = pa.table({"verdict": ["yes"], "leftover": [1], "id": ["r1"], "score": [3]})
    contribution = StageContribution()

    projected = _trim_to_declared_columns(place_stage(_rating_stage()), table, contribution)

    assert list(projected.column_names) == ["id", "score", "verdict"]
    assert contribution.dropped_columns == ["leftover"]

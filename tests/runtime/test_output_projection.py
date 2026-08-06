from __future__ import annotations

import pandas as pd
import pytest

from app.models import parse_stage, Stage
from app.runtime.manifest import StageContribution
from app.runtime.stages.execution import _project_onto_declared_columns


def _rating_stage() -> Stage:
    """A row-mapped stage declaring three output columns. The id is distinct
    from every column name so a message can be checked for both."""
    return parse_stage({
        "id": "rate", "description": "Rate", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": {"columns": [{"name": "id", "type": "str", "nullable": True}]}}],
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
    frame = pd.DataFrame({"id": ["r1"], "leftover": [1]})

    with pytest.raises(ValueError) as excinfo:
        _project_onto_declared_columns(frame, _rating_stage(), StageContribution())

    message = str(excinfo.value)
    assert "rate" in message
    assert "score" in message and "verdict" in message   # every missing one, not just the first


def test_projection_keeps_declared_order_and_reports_what_it_dropped():
    frame = pd.DataFrame({"verdict": ["yes"], "leftover": [1], "id": ["r1"], "score": [3]})
    contribution = StageContribution()

    projected = _project_onto_declared_columns(frame, _rating_stage(), contribution)

    assert list(projected.columns) == ["id", "score", "verdict"]
    assert contribution.dropped_columns == ["leftover"]

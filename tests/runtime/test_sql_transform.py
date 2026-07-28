"""The sql_transform handler: DuckDB tables named by upstream stage id, one
query in, one output frame out, plus the loud-failure paths."""
from __future__ import annotations

import pandas as pd
import pytest

from app.models import Stage
from app.runtime.stages import HANDLERS
from app.runtime.stages.sql_transform import handle_sql_transform
from app.runtime.validation import validate_dataframe
from conftest import make_run_context

_STR_COL = {"name": "specific_issues", "type": "str", "nullable": False}
_INT_COL = {"name": "n", "type": "int", "nullable": False}


def _sql_stage(query: str, input_ids: list[str], output_columns: list[dict]) -> Stage:
    return Stage.model_validate({
        "id": "q", "type": "sql_transform", "name": "q",
        "inputs": [
            {"id": sid, "schema": {"columns": [_STR_COL]}} for sid in input_ids
        ],
        "output_schema": {"columns": output_columns},
        "sql": {"query": query},
    })


def _run(stage: Stage, inputs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    handler = HANDLERS[stage.type]
    out = handler.execute(stage, inputs, make_run_context())
    assert out is not None
    return out


def test_single_input_select():
    stage = _sql_stage("SELECT * FROM src", ["src"], [_STR_COL])
    src = pd.DataFrame({"specific_issues": ["a", "b"]})
    out = _run(stage, {"src": src})
    assert list(out["specific_issues"]) == ["a", "b"]


def test_where_filters_rows():
    stage = _sql_stage(
        "SELECT * FROM src WHERE specific_issues ILIKE '%venezuela%'", ["src"], [_STR_COL]
    )
    src = pd.DataFrame({"specific_issues": ["Venezuela sanctions", "unrelated"]})
    out = _run(stage, {"src": src})
    assert list(out["specific_issues"]) == ["Venezuela sanctions"]


def test_union_all_across_two_inputs():
    stage = _sql_stage(
        "SELECT * FROM a UNION ALL SELECT * FROM b", ["a", "b"], [_STR_COL]
    )
    out = _run(stage, {
        "a": pd.DataFrame({"specific_issues": ["x"]}),
        "b": pd.DataFrame({"specific_issues": ["y"]}),
    })
    assert sorted(out["specific_issues"]) == ["x", "y"]


def test_group_by_count_distinct_and_sum():
    stage = _sql_stage(
        "SELECT client, COUNT(DISTINCT registrant) AS n_registrants, "
        "SUM(amount) AS total FROM src GROUP BY client",
        ["src"],
        [
            {"name": "client", "type": "str", "nullable": False},
            {"name": "n_registrants", "type": "int", "nullable": False},
            {"name": "total", "type": "int", "nullable": False},
        ],
    )
    src = pd.DataFrame({
        "client": ["c1", "c1", "c1", "c2"],
        "registrant": ["r1", "r1", "r2", "r1"],
        "amount": [10, 20, 30, 40],
    })
    out = _run(stage, {"src": src}).sort_values("client").reset_index(drop=True)
    assert list(out["client"]) == ["c1", "c2"]
    assert list(out["n_registrants"]) == [2, 1]
    assert list(out["total"]) == [60, 40]


def test_join_across_two_inputs():
    stage = _sql_stage(
        "SELECT a.k, a.v AS v_a, b.v AS v_b FROM a JOIN b ON a.k = b.k",
        ["a", "b"],
        [
            {"name": "k", "type": "str", "nullable": False},
            {"name": "v_a", "type": "int", "nullable": False},
            {"name": "v_b", "type": "int", "nullable": False},
        ],
    )
    out = _run(stage, {
        "a": pd.DataFrame({"k": ["x", "y"], "v": [1, 2]}),
        "b": pd.DataFrame({"k": ["x", "y"], "v": [10, 20]}),
    })
    assert list(out.sort_values("k")["v_a"]) == [1, 2]
    assert list(out.sort_values("k")["v_b"]) == [10, 20]


def test_reference_to_undeclared_table_is_rejected_at_stage_construction():
    with pytest.raises(Exception, match="not one of this stage's declared inputs"):
        _sql_stage("SELECT * FROM ghost", ["src"], [_STR_COL])


def test_malformed_query_raises_naming_the_stage():
    stage = _sql_stage("SELECT * FROM src", ["src"], [_STR_COL])
    stage = stage.model_copy(update={"sql": stage.sql.model_copy(update={"query": "not sql"})})
    with pytest.raises(ValueError, match="stage 'q'"):
        handle_sql_transform(stage, {"src": pd.DataFrame({"specific_issues": ["a"]})}, make_run_context())


def test_output_columns_disagreeing_with_output_schema_are_caught_by_validation():
    """The handler itself does no schema check — the runtime's existing
    output validation (app.runtime.validation.validate_dataframe), the one
    every stage type goes through, is what catches this."""
    stage = _sql_stage("SELECT specific_issues AS wrong_name FROM src", ["src"], [_STR_COL])
    out = _run(stage, {"src": pd.DataFrame({"specific_issues": ["a"]})})
    report = validate_dataframe(out, stage.output_schema, stage_id=stage.id, phase="output")
    assert not report.ok

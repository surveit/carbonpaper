"""The view-model that turns a linear trace + compiled stages into the
chronological story/graph payload the template renders."""
from __future__ import annotations

from app.models import Stage
from app.runtime.trace_view import build_trace_view


def _stage(data: dict) -> Stage:
    return Stage.model_validate(data)


def _stages() -> dict[str, Stage]:
    return {
        "seeds": _stage({"id": "seeds", "type": "input_data", "name": "Load seeds",
                         "connector": {"kind": "file"}}),
        "enrich": _stage({
            "id": "enrich", "type": "python_row_function", "name": "Enrich",
            "inputs": [{"id": "seeds"}],
            "function": {"kind": "inline", "code": "def transform(row):\n    return row"},
        }),
    }


def _trace() -> dict:
    # trace_to_dict shape: steps newest-first, end at the origin.
    return {
        "run_id": "R1", "start_stage": "enrich", "start_row": 0,
        "steps": [
            {"stage_id": "enrich", "stage_type": "python_row_function", "row_ordinal": 0,
             "row": {"facility_id": "a", "score": 1}, "columns_new": ["score"], "origin": "computed"},
            {"stage_id": "seeds", "stage_type": "input_data", "row_ordinal": 0,
             "row": {"facility_id": "a"}, "columns_new": ["facility_id"], "origin": "source"},
        ],
        "end": {"reached_origin": True, "at_stage": "seeds",
                "message": "input_data stage — the rows originate here"},
    }


def test_nodes_are_chronological_source_first_claim_last():
    view = build_trace_view(_trace(), _stages())
    assert [n["stage_id"] for n in view["nodes"]] == ["seeds", "enrich"]
    assert view["nodes"][0]["role"] == "source"
    assert view["nodes"][-1]["role"] == "claim"


def test_nodes_and_edges_carry_step_numbers():
    view = build_trace_view(_trace(), _stages())
    assert [n["step"] for n in view["nodes"]] == [1, 2]  # 1-based, chronological
    assert (view["edges"][0]["from_step"], view["edges"][0]["to_step"]) == (1, 2)


def test_python_node_shows_full_inline_code_not_a_reference():
    view = build_trace_view(_trace(), _stages())
    detail = view["nodes"][-1]["transform"]["detail"]
    assert detail == "def transform(row):\n    return row"  # the whole function, verbatim


def test_node_carries_transform_detail_from_compiled_stage():
    view = build_trace_view(_trace(), _stages())
    enrich = view["nodes"][-1]
    assert enrich["transform"]["kind"] == "python"
    assert "def transform(row)" in enrich["transform"]["detail"]


def test_edges_connect_consecutive_and_carry_the_source_row():
    view = build_trace_view(_trace(), _stages())
    assert len(view["edges"]) == 1
    edge = view["edges"][0]
    assert (edge["from"], edge["to"]) == ("seeds", "enrich")
    assert edge["data_row"] == {"facility_id": "a"}  # the row flowing forward


def test_trace_shows_instructions_and_data():
    stages = _stages()
    stages["score"] = _stage({
        "id": "score", "type": "llm_transform", "name": "Score",
        "inputs": [{"id": "enrich", "schema": {
            "columns": [{"name": "facility_id", "type": "str"}, {"name": "score", "type": "int"}],
            "primary_key": ["facility_id"]}}],
        "output_schema": {
            "columns": [{"name": "facility_id", "type": "str"}, {"name": "score", "type": "int"},
                        {"name": "rating", "type": "int", "nullable": False}],
            "primary_key": ["facility_id"]},
        "llm": {"prompt_instructions": "Rate for relevance.",
                "prompt_data_template": "Score: {score}"},
    })
    trace = _trace()
    trace["steps"].insert(0, {
        "stage_id": "score", "stage_type": "llm_transform", "row_ordinal": 0,
        "row": {"facility_id": "a", "score": 1, "rating": 5}, "columns_new": ["rating"],
        "origin": "computed",
    })
    view = build_trace_view(trace, stages)
    detail = view["nodes"][-1]["transform"]["detail"]
    assert detail == {"instructions": "Rate for relevance.", "data_template": "Score: {score}"}


def test_clean_origin_is_not_truncated():
    view = build_trace_view(_trace(), _stages())
    assert view["upstream"]["truncated"] is False


def test_stop_end_marks_upstream_truncated():
    trace = _trace()
    trace["steps"] = trace["steps"][:1]  # only the enrich step
    trace["end"] = {"reached_origin": False, "at_stage": "enrich",
                    "message": "stops at enrich — it reshapes rows (issue #58)"}
    view = build_trace_view(trace, _stages())
    assert view["upstream"]["truncated"] is True
    assert "#58" in view["upstream"]["message"]
    assert view["nodes"][0]["role"] == "claim"  # single node is the claim


def test_missing_compiled_stage_degrades_gracefully():
    view = build_trace_view(_trace(), {})  # no compiled stages at all
    assert view["nodes"][-1]["transform"]["kind"] == "unknown"
    assert view["nodes"][-1]["transform"]["detail"] is None

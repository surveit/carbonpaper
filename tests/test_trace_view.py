"""The web view-model that turns a linear trace + compiled stages into the
chronological story/graph payload the template renders."""
from __future__ import annotations

from app.models import Stage
from app.web.trace_view import build_trace_view


def _stage(data: dict) -> Stage:
    return Stage.model_validate(data)


def _stages() -> dict[str, Stage]:
    return {
        "seeds": _stage({"id": "seeds", "type": "input_data", "name": "Load seeds",
                         "connector": {"kind": "computed_static"}}),
        "enrich": _stage({
            "id": "enrich", "type": "python_row_function", "name": "Enrich",
            "inputs": [{"id": "seeds"}],
            "function": {"kind": "inline", "code": "def f(row):\n    return row"},
        }),
    }


def _trace() -> dict:
    # trace_to_dict shape: hops newest-first, terminal at the origin.
    return {
        "run_id": "R1", "start_stage": "enrich", "start_row": 0,
        "hops": [
            {"stage_id": "enrich", "stage_type": "python_row_function", "row_ordinal": 0,
             "row": {"facility_id": "a", "score": 1}, "columns_new": ["score"], "origin": "computed"},
            {"stage_id": "seeds", "stage_type": "input_data", "row_ordinal": 0,
             "row": {"facility_id": "a"}, "columns_new": ["facility_id"], "origin": "source"},
        ],
        "terminal": {"kind": "origin", "stage_id": "seeds",
                     "message": "input_data stage — the rows originate here"},
    }


def test_nodes_are_chronological_source_first_claim_last():
    view = build_trace_view(_trace(), _stages())
    assert [n["stage_id"] for n in view["nodes"]] == ["seeds", "enrich"]
    assert view["nodes"][0]["role"] == "source"
    assert view["nodes"][-1]["role"] == "claim"


def test_node_carries_transform_detail_from_compiled_stage():
    view = build_trace_view(_trace(), _stages())
    enrich = view["nodes"][-1]
    assert enrich["transform"]["kind"] == "python"
    assert "def f(row)" in enrich["transform"]["detail"]


def test_edges_connect_consecutive_and_carry_the_source_row():
    view = build_trace_view(_trace(), _stages())
    assert len(view["edges"]) == 1
    edge = view["edges"][0]
    assert (edge["from"], edge["to"]) == ("seeds", "enrich")
    assert edge["data_row"] == {"facility_id": "a"}  # the row flowing forward


def test_clean_origin_is_not_truncated():
    view = build_trace_view(_trace(), _stages())
    assert view["upstream"]["truncated"] is False


def test_stop_terminal_marks_upstream_truncated():
    trace = _trace()
    trace["hops"] = trace["hops"][:1]  # only the enrich hop
    trace["terminal"] = {"kind": "llm_transform", "stage_id": "enrich",
                         "message": "llm_transform is 1:1 only once PR #29 lands (issue #61)"}
    view = build_trace_view(trace, _stages())
    assert view["upstream"]["truncated"] is True
    assert "#61" in view["upstream"]["message"]
    assert view["nodes"][0]["role"] == "claim"  # single node is the claim


def test_missing_compiled_stage_degrades_gracefully():
    view = build_trace_view(_trace(), {})  # no compiled stages at all
    assert view["nodes"][-1]["transform"]["kind"] == "unknown"
    assert view["nodes"][-1]["transform"]["detail"] is None

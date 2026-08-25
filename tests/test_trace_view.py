"""The view-model that turns a linear trace + compiled stages into the
chronological story/graph payload the template renders."""
from __future__ import annotations

from app.models import Workflow, WorkflowStage, parse_stage, Stage
from app.web.panel_links import AppPanelLinks
from app.web.trace_view import build_trace_view


def _stage(data: dict) -> Stage:
    return parse_stage(data)


def _view(trace: dict, stages: dict[str, WorkflowStage]) -> dict:
    return build_trace_view(trace, stages, AppPanelLinks("proj", "R1"))


# The columns the traced rows carry: seeds emits facility_id, enrich adds score.
# Every input declares the schema it expects and every non-publish stage declares
# its output_schema (app/models/stage.py: Stage._schemas_declared).
_SEEDS_SCHEMA = {"columns": [{"name": "facility_id", "type": "str", "nullable": True}]}
_ENRICH_SCHEMA = {"columns": [{"name": "facility_id", "type": "str", "nullable": True},
                              {"name": "score", "type": "int", "nullable": True}]}


def _stages() -> dict[str, WorkflowStage]:
    return Workflow(stages=list(_authored_stages().values())).index_workflow_stages_by_id()


def _authored_stages() -> dict[str, Stage]:
    return {
        "seeds": _stage({"id": "seeds", "type": "input_data", "description": "Load seeds",
                         "connector": {"kind": "file"},
                         "signature": {"form": "replaces", "produces": _SEEDS_SCHEMA["columns"]}}),
        "enrich": _stage({
            "id": "enrich", "type": "python_row_function", "description": "Enrich",
            "inputs": [{"id": "seeds"}],
            "signature": {
                "form": "extends",
                "reads": [{"input": "seeds", "columns": _SEEDS_SCHEMA["columns"]}],
                "adds": [{"name": "score", "type": "int", "nullable": True}],
            },
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
    view = _view(_trace(), _stages())
    assert [n["stage_id"] for n in view["nodes"]] == ["seeds", "enrich"]
    assert view["nodes"][0]["role"] == "source"
    assert view["nodes"][-1]["role"] == "claim"


def test_nodes_and_edges_carry_step_numbers():
    view = _view(_trace(), _stages())
    assert [n["step"] for n in view["nodes"]] == [1, 2]  # 1-based, chronological
    assert (view["edges"][0]["from_step"], view["edges"][0]["to_step"]) == (1, 2)


def test_python_node_shows_full_inline_code_not_a_reference():
    view = _view(_trace(), _stages())
    detail = view["nodes"][-1]["transform"]["detail"]
    assert detail == "def transform(row):\n    return row"  # the whole function, verbatim


def test_node_carries_transform_detail_from_compiled_stage():
    view = _view(_trace(), _stages())
    enrich = view["nodes"][-1]
    assert enrich["transform"]["kind"] == "python"
    assert "def transform(row)" in enrich["transform"]["detail"]


def test_edges_connect_consecutive_and_carry_the_source_row():
    view = _view(_trace(), _stages())
    assert len(view["edges"]) == 1
    edge = view["edges"][0]
    assert (edge["from"], edge["to"]) == ("seeds", "enrich")
    assert edge["data_row"] == {"facility_id": "a"}  # the row flowing forward


def test_trace_shows_instructions_and_data():
    authored = _authored_stages()
    authored["score"] = _stage({
        "id": "score", "type": "llm_transform", "description": "Score",
        "inputs": [{"id": "enrich"}],
        "signature": {
            "form": "extends",
            "reads": [
                {
                    "input": "enrich",
                    "columns": [{"name": "score", "type": "int", "nullable": True}],
                },
            ],
            "adds": [{"name": "rating", "type": "int", "nullable": False}],
        },
        "llm": {"prompt_instructions": "Rate for relevance.",
                "prompt_data_template": "Score: {score}"},
    })
    stages = Workflow(stages=list(authored.values())).index_workflow_stages_by_id()
    trace = _trace()
    trace["steps"].insert(0, {
        "stage_id": "score", "stage_type": "llm_transform", "row_ordinal": 0,
        "row": {"facility_id": "a", "score": 1, "rating": 5}, "columns_new": ["rating"],
        "origin": "computed",
    })
    view = _view(trace, stages)
    detail = view["nodes"][-1]["transform"]["detail"]
    assert detail == {"instructions": "Rate for relevance.", "data_template": "Score: {score}"}


def test_clean_origin_is_not_truncated():
    view = _view(_trace(), _stages())
    assert view["upstream"]["truncated"] is False


def test_stop_end_marks_upstream_truncated():
    trace = _trace()
    trace["steps"] = trace["steps"][:1]  # only the enrich step
    trace["end"] = {"reached_origin": False, "at_stage": "enrich",
                    "message": "stops at enrich — it reshapes rows (issue #58)"}
    view = _view(trace, _stages())
    assert view["upstream"]["truncated"] is True
    assert "#58" in view["upstream"]["message"]
    assert view["nodes"][0]["role"] == "claim"  # single node is the claim


def test_missing_compiled_stage_degrades_gracefully():
    view = _view(_trace(), {})  # no compiled stages at all
    node = view["nodes"][-1]
    assert node["transform"]["kind"] == "unknown"
    assert node["transform"]["detail"] is None
    # The row and its marks come off the run's own outputs, so an unreadable
    # workflow version costs the reader the transform, never the data.
    assert [f["name"] for f in node["row_diff"]["columns"]] == ["facility_id", "score"]


def test_a_node_carries_its_row_marked_against_its_parents():
    view = _view(_trace(), _stages())
    diff = view["nodes"][-1]["row_diff"]
    assert diff["added"] == 1 and diff["changed"] == 0
    assert [(f["name"], f["state"]) for f in diff["columns"]] == [
        ("facility_id", "carried"), ("score", "added"),
    ]


def test_a_node_names_the_row_its_diff_was_taken_against():
    view = _view(_trace(), _stages())
    assert view["nodes"][0]["base"] is None  # the origin has no parent to compare to
    assert view["nodes"][-1]["base"] == {
        "stage_id": "seeds", "row_ordinal": 0, "row_number": "1"}


def test_a_node_links_where_the_reader_goes_next():
    view = _view(_trace(), _stages())
    links = view["nodes"][-1]["links"]
    assert links["stage"] == "/project/proj/runs/R1#enrich"
    assert links["rows"] == "/project/proj/runs/R1/stage/enrich/rows"
    assert links["trace"] == "/project/proj/runs/R1/stage/enrich/row/0/trace/view"


def test_a_branch_carries_its_own_links_so_the_page_builds_no_urls():
    trace = _trace()
    trace["steps"][0]["branches"] = [
        {"stage_id": "aliases", "row_ordinal": 7, "kind": "match", "columns": None},
    ]
    branch = _view(trace, _stages())["nodes"][-1]["branches"][0]
    assert branch["links"]["trace"] == "/project/proj/runs/R1/stage/aliases/row/7/trace/view"

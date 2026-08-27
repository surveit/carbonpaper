"""Tests for app/models/workflow.py — the Workflow model and its graph checks."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app import models as m
from app.models import parse_stage

_K = {"columns": [{"name": "k", "type": "str", "nullable": True}]}


def S(**kw):
    kw.setdefault("description", kw.get("id", "x"))
    return kw


def _in(id_, schema=_K):
    return {"id": id_}


def test_workflow_clean(tmp_path):
    wf = m.parse_workflow([
        S(id="load", type="input_data", signature={"form": "replaces", "produces": _K["columns"]},
          connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv"), "format": "csv"}}),
        S(id="extract", type="python_frame_function", inputs=[_in("load")],
          function={"kind": "inline", "code": "def transform(row): return row"},
          signature={
              "form": "replaces",
              "reads": [{"input": "load", "columns": _K["columns"]}],
              "produces": _K["columns"],
          }),
    ])
    assert [s.id for s in wf.stages] == ["load", "extract"]


def test_workflow_duplicate_ids(tmp_path):
    with pytest.raises(ValidationError):
        m.parse_workflow([
            S(id="a", type="input_data", signature={"form": "replaces", "produces": _K["columns"]},
              connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv")}}),
            S(id="a", type="input_data", signature={"form": "replaces", "produces": _K["columns"]},
              connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv")}}),
        ])


def test_workflow_dangling_input():
    with pytest.raises(ValidationError):
        m.parse_workflow([
            S(id="b", type="python_frame_function", inputs=[_in("ghost")],
              function={"kind": "inline", "code": "def transform(row): return row"},
              signature={
                  "form": "replaces",
                  "reads": [{"input": "ghost", "columns": _K["columns"]}],
                  "produces": _K["columns"],
              }),
        ])


def test_workflow_cycle():
    with pytest.raises(ValidationError):
        m.parse_workflow([
            S(id="a", type="python_frame_function", inputs=[_in("b")], signature={
                "form": "replaces",
                "reads": [{"input": "b", "columns": _K["columns"]}],
                "produces": _K["columns"],
            },
              function={"kind": "inline", "code": "def transform(row): return row"}),
            S(id="b", type="python_frame_function", inputs=[_in("a")], signature={
                "form": "replaces",
                "reads": [{"input": "a", "columns": _K["columns"]}],
                "produces": _K["columns"],
            },
              function={"kind": "inline", "code": "def transform(row): return row"}),
        ])


# the graph checks are plain functions — test them directly (the point of the split).
# Each RETURNS its issues (all of them) rather than raising on the first.
def test_validate_inputs_resolve_reports_all_dangling():
    s = parse_stage(S(id="b", type="enrich",
                               inputs=[_in("ghost1", {"columns": [{"name": "x", "type": "str", "nullable": True}]}),
                                       _in("ghost2", {"columns": [{"name": "y", "type": "str", "nullable": True}]})],
                               join={"keys": [{"left": "x", "right": "y"}], "enrich_with": {"y": "y"}},
                               signature={
                                   "form": "extends",
                                   "reads": [
                                       {"input": "ghost1", "columns": _X["columns"]},
                                       {"input": "ghost2", "columns": _Y["columns"]},
                                   ],
                                   "adds": _Y["columns"],
                               }))
    issues = m.validate_inputs_resolve([s])
    assert len(issues) == 2  # both dangling inputs, not just the first
    assert all("references no stage" in i for i in issues)


def test_detect_cycle_reports_cycle():
    a = parse_stage(S(id="a", type="python_frame_function", inputs=[_in("b")], signature={
        "form": "replaces",
        "reads": [{"input": "b", "columns": _K["columns"]}],
        "produces": _K["columns"],
    },
                               function={"kind": "inline", "code": "def transform(row): return row"}))
    b = parse_stage(S(id="b", type="python_frame_function", inputs=[_in("a")], signature={
        "form": "replaces",
        "reads": [{"input": "a", "columns": _K["columns"]}],
        "produces": _K["columns"],
    },
                               function={"kind": "inline", "code": "def transform(row): return row"}))
    assert m.detect_cycle([a, b])  # non-empty


def test_detect_cycle_empty_when_acyclic(tmp_path):
    a = parse_stage(S(id="a", type="input_data", signature={"form": "replaces", "produces": _K["columns"]},
                               connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv")}}))
    b = parse_stage(S(id="b", type="python_frame_function", inputs=[_in("a")], signature={
        "form": "replaces",
        "reads": [{"input": "a", "columns": _K["columns"]}],
        "produces": _K["columns"],
    },
                               function={"kind": "inline", "code": "def transform(row): return row"}))
    assert m.detect_cycle([a, b]) == []


# validate_workflow is the non-fatal aggregate entry: it runs every cross-stage
# check on already-validated stages and returns all issues at once ([] means clean).
def test_validate_workflow_clean_is_empty(tmp_path):
    stages = [
        parse_stage(S(id="load", type="input_data", signature={"form": "replaces", "produces": _K["columns"]},
                               connector={"kind": "file",
                                          "params": {"path": str(tmp_path / "d.csv"), "format": "csv"}})),
    ]
    assert m.validate_workflow(stages) == []


def test_validate_workflow_reports_issues():
    s = parse_stage(S(id="j", type="enrich",
                               inputs=[_in("a", {"columns": [{"name": "x", "type": "str", "nullable": True}]}),
                                       _in("b", {"columns": [{"name": "y", "type": "str", "nullable": True}]})],
                               join={"keys": [{"left": "x", "right": "y"}], "enrich_with": {"y": "y"}},
                               signature={
                                   "form": "extends",
                                   "reads": [
                                       {"input": "a", "columns": _X["columns"]},
                                       {"input": "b", "columns": _Y["columns"]},
                                   ],
                                   "adds": _Y["columns"],
                               }))
    issues = m.validate_workflow([s])
    assert issues  # both inputs dangle — reported, not raised


# ── llm_transform 1:1 eligibility (enforced by Stage construction, not here) ──
# The invariant lives on the Stage model, so an ineligible stage fails to
# construct — these assert the rejection at model_validate / parse_workflow.
def _llm_1to1_dict(**over):
    base = dict(
        id="score", type="llm_transform", inputs=[{"id": "load"}],
        signature={
            "form": "extends",
            "reads": [
                {
                    "input": "load",
                    "columns": [{"name": "text", "type": "str", "nullable": True}],
                },
            ],
            "adds": [{"name": "score", "type": "int", "nullable": True}],
        },
        llm={"prompt_template": "score {text}"},
    )
    base.update(over)
    return S(**base)


def test_llm_transform_valid_1to1_constructs():
    assert parse_stage(_llm_1to1_dict()).id == "score"


def test_llm_transform_rewriting_a_column_rejected():
    with pytest.raises(ValidationError, match="rewrites are not supported"):
        parse_stage(_llm_1to1_dict(signature={
            "form": "extends",
            "reads": [{"input": "load",
                       "columns": [{"name": "text", "type": "str", "nullable": True}]}],
            "rewrites": [{"name": "text", "type": "int", "nullable": True}],
            "adds": [{"name": "score", "type": "int", "nullable": True}],
        }))


def test_llm_transform_adds_nothing_rejected():
    with pytest.raises(ValidationError, match="adds no columns"):
        parse_stage(_llm_1to1_dict(signature={
            "form": "extends",
            "reads": [{"input": "load",
                       "columns": [{"name": "text", "type": "str", "nullable": True}]}],
        }))


def test_parse_workflow_rejects_ineligible_llm_transform():
    bad = _llm_1to1_dict(signature={
            "form": "extends",
            "reads": [{"input": "load",
                       "columns": [{"name": "text", "type": "str", "nullable": True}]}],
        })
    with pytest.raises(ValidationError, match="adds no columns"):
        m.parse_workflow([bad])


# ── What a stage reads must be satisfied by its upstream ─────────────────────
# There is no stored input schema left to disagree with anything: a stage declares
# what it READS, and the check is that the upstream's resolved output supplies it.
def _producer(**over):
    base = dict(
        id="up", type="input_data",
        connector={"kind": "file"},
        signature={
            "form": "replaces",
            "produces": [
                {"name": "id", "type": "str", "nullable": True},
                {"name": "text", "type": "str", "nullable": True},
                {"name": "score", "type": "int", "nullable": True},
            ],
        },
    )
    base.update(over)
    return S(**base)


def _consumer(read_columns, **over):
    base = dict(
        id="down", type="python_frame_function",
        inputs=[{"id": "up"}],
        function={"kind": "inline", "code": "def transform(df): return df"},
        signature={
            "form": "replaces",
            "reads": [{"input": "up", "columns": read_columns["columns"]}],
            "produces": read_columns["columns"],
        },
    )
    base.update(over)
    return S(**base)


def test_reads_are_clean_when_they_name_the_whole_upstream_output():
    assert m.validate_workflow_draft([
        _producer(),
        _consumer({"columns": [{"name": "id", "type": "str", "nullable": True},
                               {"name": "text", "type": "str", "nullable": True},
                               {"name": "score", "type": "int", "nullable": True}]}),
    ]) == []


def test_reads_are_clean_when_they_name_a_projection():
    assert m.validate_workflow_draft([
        _producer(),
        _consumer({"columns": [{"name": "score", "type": "int", "nullable": True}]}),
    ]) == []


def test_reading_a_column_the_upstream_does_not_supply_is_flagged():
    issues = m.validate_workflow(
        [parse_stage(_producer()),
         parse_stage(_consumer({"columns": [{"name": "quote", "type": "str", "nullable": True}]}))])
    assert len(issues) == 1
    assert "down" in issues[0] and "up" in issues[0] and "quote" in issues[0]


def test_a_non_null_producer_satisfies_a_nullable_read():
    assert m.validate_workflow_draft([
        _producer(signature={"form": "replaces", "produces": [
            {"name": "id", "type": "str", "nullable": True},
            {"name": "score", "type": "int", "nullable": False}]}),
        _consumer({"columns": [{"name": "score", "type": "int", "nullable": True}]}),
    ]) == []


def test_a_nullable_producer_does_not_satisfy_a_non_null_read():
    issues = m.validate_workflow([
        parse_stage(_producer(signature={"form": "replaces", "produces": [
            {"name": "id", "type": "str", "nullable": True},
            {"name": "score", "type": "int", "nullable": True}]})),
        parse_stage(_consumer(
            {"columns": [{"name": "score", "type": "int", "nullable": False}]})),
    ])
    assert len(issues) == 1
    assert "score" in issues[0] and "nullable" in issues[0]


def test_a_read_of_the_wrong_type_is_flagged():
    issues = m.validate_workflow([
        parse_stage(_producer()),
        parse_stage(_consumer({"columns": [{"name": "score", "type": "str", "nullable": True}]}))])
    assert len(issues) == 1
    assert "score" in issues[0] and "type" in issues[0]


def _report_upstream_stages():
    """Built without the graph validator, which would reject the input from `pub`."""
    return [
        parse_stage(_producer()),
        parse_stage(
            S(id="pub", type="report",
              inputs=[{"id": "up"}],
              report={"format": "json"}, signature={"form": "replaces"},
              function={"kind": "inline",
                        "code": "def transform(df, output_dir): return df"})),
        parse_stage(
            _consumer({"columns": [{"name": "anything", "type": "str", "nullable": True}]}, id="down",
                      inputs=[{"id": "pub"}])),
    ]


def test_resolution_raises_on_an_upstream_resolving_no_output():
    with pytest.raises(ValueError, match="resolves no output schema"):
        m.resolve_workflow_stages(_report_upstream_stages())


def test_graph_issues_reports_a_report_upstream_instead_of_raising():
    issues = m.validate_workflow(_report_upstream_stages())
    assert len(issues) == 1
    assert "down" in issues[0] and "pub" in issues[0] and "report stage" in issues[0]


def test_resolution_raises_on_an_input_naming_no_stage():
    stages = [parse_stage(_consumer({"columns": [{"name": "id", "type": "str", "nullable": True}]}))]
    with pytest.raises(ValueError, match="references no stage"):
        m.resolve_workflow_stages(stages)


def test_graph_issues_reports_a_dangling_input_instead_of_raising():
    issues = m.validate_workflow(
        [parse_stage(_consumer({"columns": [{"name": "id", "type": "str", "nullable": True}]}))])
    assert issues == ["`down`: input `up` references no stage"]


# ── A report stage produces no table, so nothing may read it ────────────────
def _publish(stage_id="pub", inputs=("load",)):
    return S(id=stage_id, type="report", inputs=[_in(i) for i in inputs],
             report={"format": "json"}, signature={"form": "replaces"},
             function={"kind": "inline", "code": "def transform(df, output_dir): return df"})


_X = {"columns": [{"name": "x", "type": "str", "nullable": True}]}
_Y = {"columns": [{"name": "y", "type": "str", "nullable": True}]}


def _reader(stage_id, upstream):
    return S(id=stage_id, type="python_frame_function", inputs=[_in(upstream)],
             function={"kind": "inline", "code": "def transform(df): return df"},
             signature={"form": "replaces", "produces": _K["columns"]})


def _loader():
    return S(id="load", type="input_data", connector={"kind": "file"}, signature={"form": "replaces", "produces": _K["columns"]})


def test_validate_report_is_terminal_flags_stage_reading_a_publish():
    stages = [parse_stage(s) for s in
              (_loader(), _publish(), _reader("down", "pub"))]
    issues = m.validate_report_is_terminal(stages)
    assert len(issues) == 1
    assert "down" in issues[0] and "pub" in issues[0]


def test_validate_report_is_terminal_reports_every_offending_edge():
    stages = [parse_stage(s) for s in (
        _loader(), _publish("pub_a"), _publish("pub_b"),
        _reader("down_a", "pub_a"), _reader("down_b", "pub_b"),
        S(id="down_c", type="enrich", inputs=[_in("pub_a", _X), _in("pub_b", _Y)],
          join={"keys": [{"left": "x", "right": "y"}], "enrich_with": {"y": "y"}},
          signature={
              "form": "extends",
              "reads": [
                  {"input": "pub_a", "columns": _X["columns"]},
                  {"input": "pub_b", "columns": _Y["columns"]},
              ],
              "adds": _Y["columns"],
          }),
    )]
    issues = m.validate_report_is_terminal(stages)
    assert len(issues) == 4  # every offending edge in one pass, not just the first


def test_validate_report_is_terminal_clean_when_the_report_is_terminal():
    stages = [parse_stage(s) for s in (_loader(), _publish())]
    assert m.validate_report_is_terminal(stages) == []


def test_validate_report_is_terminal_clean_with_several_unconsumed_publishes():
    stages = [parse_stage(s) for s in
              (_loader(), _publish("pub_a"), _publish("pub_b"), _publish("pub_c"))]
    assert m.validate_report_is_terminal(stages) == []


# ── Which stages a report carries (find_stages_reaching_report) ─────────────

def test_find_stages_reaching_report_takes_the_direct_feeder():
    stages = [parse_stage(s) for s in (_loader(), _publish())]
    assert m.find_stages_reaching_report(stages) == {"load"}


def test_find_stages_reaching_report_leaves_out_the_report_stage_itself():
    stages = [parse_stage(s) for s in (_loader(), _publish())]
    assert "pub" not in m.find_stages_reaching_report(stages)


def test_find_stages_reaching_report_reaches_through_intermediate_stages():
    stages = [parse_stage(s) for s in (
        _loader(), _reader("mid", "load"), _reader("near", "mid"), _publish(inputs=("near",)),
    )]
    assert m.find_stages_reaching_report(stages) == {"load", "mid", "near"}


def test_find_stages_reaching_report_leaves_out_a_stage_nothing_published_reads():
    stages = [parse_stage(s) for s in (
        _loader(), _reader("checked", "load"), _publish(inputs=("load",)),
    )]
    assert m.find_stages_reaching_report(stages) == {"load"}


def test_find_stages_reaching_report_is_empty_without_a_report_stage():
    stages = [parse_stage(s) for s in (_loader(), _reader("mid", "load"))]
    assert m.find_stages_reaching_report(stages) == set()


def test_find_stages_reaching_report_unions_over_several_report_stages():
    stages = [parse_stage(s) for s in (
        _loader(), _reader("left", "load"), _reader("right", "load"),
        _publish("pub_a", inputs=("left",)), _publish("pub_b", inputs=("right",)),
    )]
    assert m.find_stages_reaching_report(stages) == {"load", "left", "right"}


def test_parse_workflow_rejects_stage_reading_a_publish():
    with pytest.raises(ValidationError, match="report"):
        m.parse_workflow([_loader(), _publish(), _reader("down", "pub")])


def test_parse_workflow_accepts_terminal_publish():
    wf = m.parse_workflow([_loader(), _publish()])
    assert [s.id for s in wf.stages] == ["load", "pub"]


def test_parse_workflow_rejects_nonconformant_edge():
    with pytest.raises(ValidationError, match="quote"):
        m.parse_workflow([
            _producer(),
            _consumer({"columns": [{"name": "quote", "type": "str", "nullable": True}]}),
        ])


# ─── sort_stages_by_dependency ────────────────────────────────────────────────


def _build_stage_draft(id_, inputs=()):
    from app.models import StageDraft

    return StageDraft.model_validate({
        "id": id_, "description": id_, "type": "input_data", "connector": {"kind": "file"},
        "inputs": [_in(i) for i in inputs],
    })


def test_sort_stages_by_dependency_puts_every_stage_after_its_inputs():
    order = m.workflow.sort_stages_by_dependency(
        [_build_stage_draft("c", ["b"]), _build_stage_draft("a"), _build_stage_draft("b", ["a"])]
    )
    assert [s.id for s in order] == ["a", "b", "c"]


def test_sort_stages_by_dependency_ignores_inputs_from_outside_the_given_set():
    order = m.workflow.sort_stages_by_dependency([_build_stage_draft("b", ["stored"]), _build_stage_draft("a")])
    assert [s.id for s in order] == ["b", "a"], "ties keep submission order"


def test_sort_stages_by_dependency_raises_on_a_cycle():
    with pytest.raises(ValueError, match="cyclic"):
        m.workflow.sort_stages_by_dependency([_build_stage_draft("a", ["b"]), _build_stage_draft("b", ["a"])])

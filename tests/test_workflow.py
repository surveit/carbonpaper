"""Tests for app/models/workflow.py — the Workflow model and its graph checks."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app import models as m
from app.models import Stage

_K = {"columns": [{"name": "k"}]}


def S(**kw):
    kw.setdefault("name", kw.get("id", "x"))
    return kw


def _in(id_, schema=_K):
    return {"id": id_, "schema": schema}


def test_workflow_clean(tmp_path):
    wf = m.parse_workflow([
        S(id="load", type="input_data", output_schema=_K,
          connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv"), "format": "csv"}}),
        S(id="extract", type="python_frame_function", inputs=[_in("load")],
          function={"kind": "inline", "code": "def transform(row): return row"},
          output_schema=_K),
    ])
    assert [s.id for s in wf.stages] == ["load", "extract"]


def test_workflow_duplicate_ids(tmp_path):
    with pytest.raises(ValidationError):
        m.parse_workflow([
            S(id="a", type="input_data", output_schema=_K,
              connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv")}}),
            S(id="a", type="input_data", output_schema=_K,
              connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv")}}),
        ])


def test_workflow_dangling_input():
    with pytest.raises(ValidationError):
        m.parse_workflow([
            S(id="b", type="python_frame_function", inputs=[_in("ghost")],
              function={"kind": "inline", "code": "def transform(row): return row"},
              output_schema=_K),
        ])


def test_workflow_cycle():
    with pytest.raises(ValidationError):
        m.parse_workflow([
            S(id="a", type="python_frame_function", inputs=[_in("b")], output_schema=_K,
              function={"kind": "inline", "code": "def transform(row): return row"}),
            S(id="b", type="python_frame_function", inputs=[_in("a")], output_schema=_K,
              function={"kind": "inline", "code": "def transform(row): return row"}),
        ])


# the graph checks are plain functions — test them directly (the point of the split).
# Each RETURNS its issues (all of them) rather than raising on the first.
def test_validate_inputs_resolve_reports_all_dangling():
    s = Stage.model_validate(S(id="b", type="join",
                               inputs=[_in("ghost1", {"columns": [{"name": "x"}]}),
                                       _in("ghost2", {"columns": [{"name": "y"}]})],
                               join={"keys": [{"left": "x", "right": "y"}]},
                               output_schema={"columns": [{"name": "x"}, {"name": "y"}]}))
    issues = m.validate_inputs_resolve([s])
    assert len(issues) == 2  # both dangling inputs, not just the first
    assert all("references no stage" in i for i in issues)


def test_detect_cycle_reports_cycle():
    a = Stage.model_validate(S(id="a", type="python_frame_function", inputs=[_in("b")], output_schema=_K,
                               function={"kind": "inline", "code": "def transform(row): return row"}))
    b = Stage.model_validate(S(id="b", type="python_frame_function", inputs=[_in("a")], output_schema=_K,
                               function={"kind": "inline", "code": "def transform(row): return row"}))
    assert m.detect_cycle([a, b])  # non-empty


def test_detect_cycle_empty_when_acyclic(tmp_path):
    a = Stage.model_validate(S(id="a", type="input_data", output_schema=_K,
                               connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv")}}))
    b = Stage.model_validate(S(id="b", type="python_frame_function", inputs=[_in("a")], output_schema=_K,
                               function={"kind": "inline", "code": "def transform(row): return row"}))
    assert m.detect_cycle([a, b]) == []


# validate_workflow is the non-fatal aggregate entry: it runs every cross-stage
# check on already-validated stages and returns all issues at once ([] means clean).
def test_validate_workflow_clean_is_empty(tmp_path):
    stages = [
        Stage.model_validate(S(id="load", type="input_data", output_schema=_K,
                               connector={"kind": "file",
                                          "params": {"path": str(tmp_path / "d.csv"), "format": "csv"}})),
    ]
    assert m.validate_workflow(stages) == []


def test_validate_workflow_reports_issues():
    s = Stage.model_validate(S(id="j", type="join",
                               inputs=[_in("a", {"columns": [{"name": "x"}]}),
                                       _in("b", {"columns": [{"name": "y"}]})],
                               join={"keys": [{"left": "x", "right": "y"}]},
                               output_schema={"columns": [{"name": "x"}, {"name": "y"}]}))
    issues = m.validate_workflow([s])
    assert issues  # both inputs dangle — reported, not raised


# ── llm_transform 1:1 eligibility (enforced by Stage construction, not here) ──
# The invariant lives on the Stage model, so an ineligible stage fails to
# construct — these assert the rejection at model_validate / parse_workflow.
def _llm_1to1_dict(**over):
    """Dict for a valid strictly-1:1 llm_transform: input {id(pk), text} → output adds score."""
    base = dict(
        id="score", type="llm_transform", inputs=[{
            "id": "load",
            "schema": {"columns": [{"name": "id", "type": "str"},
                                   {"name": "text", "type": "str"}],
                       "primary_key": ["id"]},
        }],
        output_schema={"columns": [{"name": "id", "type": "str"},
                                   {"name": "text", "type": "str"},
                                   {"name": "score", "type": "int"}],
                       "primary_key": ["id"]},
        llm={"prompt_template": "score {text}"},
    )
    base.update(over)
    return S(**base)


def test_llm_transform_valid_1to1_constructs():
    assert Stage.model_validate(_llm_1to1_dict()).id == "score"


def test_llm_transform_pk_mismatch_rejected():
    with pytest.raises(ValidationError, match="primary_key"):
        Stage.model_validate(_llm_1to1_dict(output_schema={
            "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"},
                        {"name": "score", "type": "int"}],
            "primary_key": ["text"]}))


def test_llm_transform_drops_input_column_rejected():
    with pytest.raises(ValidationError, match="text"):
        Stage.model_validate(_llm_1to1_dict(output_schema={
            "columns": [{"name": "id", "type": "str"}, {"name": "score", "type": "int"}],
            "primary_key": ["id"]}))  # dropped `text`


def test_llm_transform_modifies_column_schema_rejected():
    with pytest.raises(ValidationError, match="text"):
        Stage.model_validate(_llm_1to1_dict(output_schema={
            "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "int"},
                        {"name": "score", "type": "int"}],
            "primary_key": ["id"]}))  # `text` str -> int


def test_llm_transform_adds_nothing_rejected():
    with pytest.raises(ValidationError, match="adds no columns"):
        Stage.model_validate(_llm_1to1_dict(output_schema={
            "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}],
            "primary_key": ["id"]}))  # adds no new column


def test_parse_workflow_rejects_ineligible_llm_transform():
    """The load seam (parse_workflow → Stage construction) rejects a non-1:1 stage."""
    bad = _llm_1to1_dict(output_schema={
        "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}],
        "primary_key": ["id"]})
    with pytest.raises(ValidationError, match="adds no columns"):
        m.parse_workflow([bad])


# ── Edge schema conformance (validate_edge_schemas) ───────────────────────────
# A downstream stage's declared input schema (`inputs[i].schema`) is a REQUIREMENT
# — possibly a projection — that the upstream stage's declared output_schema must
# satisfy. Checked at save time as a cross-stage graph invariant.
def _producer(**over):
    """input_data stage `up` declaring an output_schema of {id, text, score}."""
    base = dict(
        id="up", type="input_data",
        connector={"kind": "file"},
        output_schema={"columns": [{"name": "id", "type": "str"},
                                   {"name": "text", "type": "str"},
                                   {"name": "score", "type": "int"}]},
    )
    base.update(over)
    return S(**base)


def _consumer(input_schema, **over):
    """python_frame_function `down` consuming `up`, declaring `input_schema`.
    Its `transform` is the identity, so it emits exactly what it consumes."""
    base = dict(
        id="down", type="python_frame_function",
        inputs=[{"id": "up", "schema": input_schema}],
        function={"kind": "inline", "code": "def transform(df): return df"},
        output_schema=input_schema,
    )
    base.update(over)
    return S(**base)


def test_check_edge_schemas_clean_when_input_is_exact_copy():
    stages = m.parse_workflow([
        _producer(),
        _consumer({"columns": [{"name": "id", "type": "str"},
                               {"name": "text", "type": "str"},
                               {"name": "score", "type": "int"}]}),
    ]).stages
    assert m.validate_edge_schemas(stages) == []


def test_check_edge_schemas_clean_when_input_is_a_projection():
    # A projection naming only the columns the stage consumes is fine (subsumption,
    # not identity) — `down` needs just `score`, `up` produces it.
    stages = m.parse_workflow([
        _producer(),
        _consumer({"columns": [{"name": "score", "type": "int"}]}),
    ]).stages
    assert m.validate_edge_schemas(stages) == []


def test_check_edge_schemas_flags_phantom_column():
    # `down` requires `quote`, which `up` does not produce — the #36 phantom.
    stages = [
        Stage.model_validate(_producer()),
        Stage.model_validate(_consumer({"columns": [{"name": "quote", "type": "str"}]})),
    ]
    issues = m.validate_edge_schemas(stages)
    assert len(issues) == 1
    assert "down" in issues[0] and "up" in issues[0] and "quote" in issues[0]


def test_check_edge_schemas_clean_when_producer_non_null_feeds_nullable_requirement():
    # The review-queue pattern: producer emits `score` non-null; the consumer's
    # input schema requires it only as nullable — a compatible (safe) edge.
    stages = m.parse_workflow([
        _producer(output_schema={"columns": [
            {"name": "id", "type": "str"},
            {"name": "score", "type": "int", "nullable": False}]}),
        _consumer({"columns": [{"name": "score", "type": "int", "nullable": True}]}),
    ]).stages
    assert m.validate_edge_schemas(stages) == []


def test_check_edge_schemas_flags_required_non_null_fed_by_nullable_producer():
    stages = [
        Stage.model_validate(_producer(output_schema={"columns": [
            {"name": "id", "type": "str"},
            {"name": "score", "type": "int", "nullable": True}]})),
        Stage.model_validate(_consumer(
            {"columns": [{"name": "score", "type": "int", "nullable": False}]})),
    ]
    issues = m.validate_edge_schemas(stages)
    assert len(issues) == 1
    assert "score" in issues[0] and "nullable" in issues[0]


def test_check_edge_schemas_flags_type_disagreement():
    stages = [
        Stage.model_validate(_producer()),
        Stage.model_validate(_consumer({"columns": [{"name": "score", "type": "str"}]})),
    ]
    issues = m.validate_edge_schemas(stages)
    assert len(issues) == 1
    assert "score" in issues[0] and "type" in issues[0]


def test_check_edge_schemas_skips_a_publish_upstream():
    # publish is the one type exempt from declaring an output_schema, and
    # nothing forbids it being another stage's input: unresolvable means
    # unknowable, never wrong, so that edge is skipped.
    stages = m.parse_workflow([
        _producer(),
        S(id="pub", type="publish",
          inputs=[{"id": "up", "schema": {"columns": [{"name": "id", "type": "str"}]}}],
          publish={"format": "json"},
          function={"kind": "inline", "code": "def transform(df, output_dir): return df"}),
        _consumer({"columns": [{"name": "anything", "type": "str"}]}, id="down",
                  inputs=[{"id": "pub", "schema": {"columns": [{"name": "anything", "type": "str"}]}}]),
    ]).stages
    assert m.validate_edge_schemas(stages) == []


def test_check_edge_schemas_raises_on_an_input_naming_no_stage():
    """A dangling input is a programming error here, not a finding: callers run
    validate_inputs_resolve first (graph_issues does), so reaching this means
    stage validation was bypassed."""
    stages = [Stage.model_validate(_consumer({"columns": [{"name": "id", "type": "str"}]}))]
    with pytest.raises(ValueError, match="references no stage"):
        m.validate_edge_schemas(stages)


def test_graph_issues_reports_a_dangling_input_instead_of_raising():
    """graph_issues short-circuits before validate_edge_schemas when an input
    dangles, so an invalid-but-reportable workflow still comes back as issues."""
    issues = m.validate_workflow(
        [Stage.model_validate(_consumer({"columns": [{"name": "id", "type": "str"}]}))])
    assert issues == ["`down`: input `up` references no stage"]


# ── A publish stage may not be another stage's input (validate_publish_is_terminal) ─
# A publish stage writes files instead of producing a table, so nothing downstream
# can read from it. It is also the one type exempt from declaring an output_schema,
# which is why such an edge would otherwise slip past validate_edge_schemas.
def _publish(stage_id="pub", inputs=("load",)):
    return S(id=stage_id, type="publish", inputs=[{"id": i} for i in inputs],
             publish={"format": "json"},
             function={"kind": "inline", "code": "def transform(df, output_dir): return df"})


def _reader(stage_id, upstream):
    return S(id=stage_id, type="python_frame_function", inputs=[{"id": upstream}],
             function={"kind": "inline", "code": "def transform(df): return df"})


def _loader():
    return S(id="load", type="input_data", connector={"kind": "file"})


def test_validate_publish_is_terminal_flags_stage_reading_a_publish():
    stages = [Stage.model_validate(s) for s in
              (_loader(), _publish(), _reader("down", "pub"))]
    issues = m.validate_publish_is_terminal(stages)
    assert len(issues) == 1
    assert "down" in issues[0] and "pub" in issues[0]


def test_validate_publish_is_terminal_reports_every_offending_edge():
    stages = [Stage.model_validate(s) for s in (
        _loader(), _publish("pub_a"), _publish("pub_b"),
        _reader("down_a", "pub_a"), _reader("down_b", "pub_b"),
        S(id="down_c", type="join", inputs=[{"id": "pub_a"}, {"id": "pub_b"}],
          join={"keys": [{"left": "x", "right": "y"}]}),
    )]
    issues = m.validate_publish_is_terminal(stages)
    assert len(issues) == 4  # every offending edge in one pass, not just the first


def test_validate_publish_is_terminal_clean_when_publish_is_terminal():
    stages = [Stage.model_validate(s) for s in (_loader(), _publish())]
    assert m.validate_publish_is_terminal(stages) == []


def test_validate_publish_is_terminal_clean_with_several_unconsumed_publishes():
    stages = [Stage.model_validate(s) for s in
              (_loader(), _publish("pub_a"), _publish("pub_b"), _publish("pub_c"))]
    assert m.validate_publish_is_terminal(stages) == []


def test_parse_workflow_rejects_stage_reading_a_publish():
    with pytest.raises(ValidationError, match="publish"):
        m.parse_workflow([_loader(), _publish(), _reader("down", "pub")])


def test_parse_workflow_accepts_terminal_publish():
    wf = m.parse_workflow([_loader(), _publish()])
    assert [s.id for s in wf.stages] == ["load", "pub"]


def test_parse_workflow_rejects_nonconformant_edge():
    """The save gate (parse_workflow → Workflow model validator → graph_issues)
    rejects a workflow whose edge is non-conformant."""
    with pytest.raises(ValidationError, match="quote"):
        m.parse_workflow([
            _producer(),
            _consumer({"columns": [{"name": "quote", "type": "str"}]}),
        ])

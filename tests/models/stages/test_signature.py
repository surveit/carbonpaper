"""The transform signature: its own shape rules, the stage-level rules
(`find_signature_issues`), and each type's config-vs-signature cross-check.
A stage without a signature is untouched — that invariant carries every stage
stored before signatures existed."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.stage import parse_stage
from app.models.stages.signature import (
    transform_input_schemas,
    transform_output_schema,
)
from app.models.workflow import parse_workflow, validate_workflow_draft
from conftest import source_stage


_EDGE = {
    "columns": [
        {"name": "price", "type": "str", "nullable": True},
        {"name": "title", "type": "str", "nullable": True},
    ],
}


def _row_function_stage(*, signature=None):
    spec = {
        "id": "clean",
        "description": "Clean prices",
        "type": "python_row_function",
        "inputs": [{"id": "bills"}],
        "function": {"kind": "inline", "code": "def transform(row):\n    return row"},
    }
    if signature is not None:
        spec["signature"] = signature
    return spec


def _issues(stage_dict) -> str:
    with pytest.raises(ValidationError) as err:
        parse_stage(stage_dict)
    return str(err.value)


def _workflow_issues(stage_dict, *sources) -> str:
    """Checks that need what an upstream supplies can only be answered by the workflow."""
    upstream = list(sources) or [source_stage("bills", _EDGE["columns"])]
    return "; ".join(validate_workflow_draft([*upstream, stage_dict]))


_JOIN_SUBJECT = [
    {"name": "state", "type": "str", "nullable": True},
    {"name": "bill", "type": "str", "nullable": True},
]
_JOIN_REFERENCE = [
    {"name": "code", "type": "str", "nullable": True},
    {"name": "region", "type": "str", "nullable": True},
]


def _join_issues(stage_dict) -> str:
    return _workflow_issues(
        stage_dict,
        source_stage("bills", _JOIN_SUBJECT),
        source_stage("states", _JOIN_REFERENCE),
    )


def _starlark_row_function_stage(*, signature=None):
    spec = {
        "id": "clean",
        "description": "Clean prices",
        "type": "starlark_row_function",
        "inputs": [{"id": "bills"}],
        "starlark": {"code": "def transform(row):\n    return row"},
    }
    if signature is not None:
        spec["signature"] = signature
    return spec


# ── shape rules on the signature itself ──────────────────────────────────────

def test_stage_without_signature_is_refused():
    msg = _issues(_row_function_stage())
    assert "signature" in msg and "Field required" in msg


def test_duplicate_read_column_rejected():
    msg = _issues(_row_function_stage(signature={
        "form": "extends",
        "reads": [{"input": "bills", "columns": [
            {"name": "price", "type": "str", "nullable": True}, {"name": "price", "type": "str", "nullable": True},
        ]}],
    }))
    assert "duplicate column 'price'" in msg


def test_add_and_rewrite_sharing_a_name_rejected():
    msg = _issues(_row_function_stage(signature={
        "form": "extends",
        "reads": [{"input": "bills", "columns": [{"name": "price", "type": "str", "nullable": True}]}],
        "adds": [{"name": "flag", "type": "bool", "nullable": True}],
        "rewrites": [{"name": "flag", "type": "str", "nullable": True}],
    }))
    assert "duplicate column 'flag'" in msg


def test_replaces_form_on_an_extends_type_rejected():
    msg = _issues(_row_function_stage(signature={
        "form": "replaces",
        "produces": [{"name": "price", "type": "str", "nullable": True}],
    }))
    assert "extends" in msg


# ── stage-level rules ────────────────────────────────────────────────────────

def test_read_from_unknown_input_rejected():
    msg = _workflow_issues(_row_function_stage(signature={
        "form": "extends",
        "reads": [{"input": "nowhere", "columns": [{"name": "price", "type": "str", "nullable": True}]}],
    }))
    assert "nowhere" in msg and "not one of this stage's inputs" in msg


def test_read_column_the_edge_does_not_supply_rejected():
    msg = _workflow_issues(_row_function_stage(signature={
        "form": "extends",
        "reads": [{"input": "bills", "columns": [{"name": "amount", "type": "int", "nullable": True}]}],
    }))
    assert "'amount'" in msg and "absent" in msg


def test_read_column_with_a_differing_spec_rejected():
    msg = _workflow_issues(_row_function_stage(signature={
        "form": "extends",
        "reads": [{"input": "bills", "columns": [{"name": "price", "type": "float", "nullable": True}]}],
    }))
    assert "'price'" in msg and "differs" in msg


def test_rewrite_without_reading_the_column_rejected():
    msg = _workflow_issues(_row_function_stage(
        signature={
            "form": "extends",
            "rewrites": [{"name": "price", "type": "float", "nullable": True}],
        },
    ))
    assert "rewrites `price` without reading it" in msg


def test_add_colliding_with_an_anchor_column_rejected():
    msg = _workflow_issues(_row_function_stage(signature={
        "form": "extends",
        "adds": [{"name": "title", "type": "str", "nullable": True}],
    }))
    assert "adds `title`" in msg and "already supplies" in msg


def test_the_signature_is_the_output_schema():
    workflow = parse_workflow([
        source_stage("bills", _EDGE["columns"]),
        _row_function_stage(signature={
            "form": "extends",
            "reads": [{"input": "bills", "columns": [{"name": "price", "type": "str", "nullable": True}]}],
            "rewrites": [{"name": "price", "type": "float", "nullable": True}],
            "adds": [{"name": "note", "type": "str", "nullable": True}],
        }),
    ])
    resolved = workflow.find_workflow_stage("clean").output_schema
    assert [(c.name, c.type) for c in resolved.columns] == [
        ("price", "float"), ("title", "str"), ("note", "str")]


def test_consistent_extends_signature_accepted():
    stage = parse_stage(_row_function_stage(
        signature={
            "form": "extends",
            "reads": [{"input": "bills", "columns": [{"name": "price", "type": "str", "nullable": True}]}],
            "rewrites": [{"name": "price", "type": "float", "nullable": True}],
            "adds": [{"name": "note", "type": "str", "nullable": True}],
        },
    ))
    assert stage.signature is not None and stage.signature.form == "extends"


def test_the_signature_feeds_the_fingerprint():
    plain = parse_stage(_row_function_stage(signature={"form": "extends"}))
    signed = parse_stage(_row_function_stage(
        signature={
            "form": "extends",
            "reads": [{"input": "bills", "columns": [{"name": "price", "type": "str", "nullable": True}]}],
            "rewrites": [{"name": "price", "type": "float", "nullable": True}],
        },
    ))
    assert plain.compute_definition_fingerprint() != signed.compute_definition_fingerprint()
    again = parse_stage(_row_function_stage(signature={"form": "extends"}))
    assert plain.compute_definition_fingerprint() == again.compute_definition_fingerprint()


def test_internal_namespace_refused_in_signature_columns():
    msg = _issues(_row_function_stage(signature={
        "form": "extends",
        "adds": [{"name": "_hidden", "type": "str", "nullable": True}],
    }))
    assert "_hidden" in msg and "reserved" in msg


# ── starlark_row_function mirrors python_row_function's extends-form rules ────

def test_starlark_stage_without_signature_is_refused():
    msg = _issues(_starlark_row_function_stage())
    assert "signature" in msg and "Field required" in msg


def test_starlark_add_colliding_with_an_anchor_column_rejected():
    msg = _workflow_issues(_starlark_row_function_stage(signature={
        "form": "extends",
        "adds": [{"name": "title", "type": "str", "nullable": True}],
    }))
    assert "adds `title`" in msg and "already supplies" in msg


def test_starlark_rewrite_without_reading_the_column_rejected():
    msg = _workflow_issues(_starlark_row_function_stage(signature={
        "form": "extends",
        "rewrites": [{"name": "price", "type": "float", "nullable": True}],
    }))
    assert "rewrites `price` without reading it" in msg


def test_starlark_signature_is_the_output_schema():
    workflow = parse_workflow([
        source_stage("bills", _EDGE["columns"]),
        _starlark_row_function_stage(signature={
            "form": "extends",
            "reads": [{"input": "bills", "columns": [{"name": "price", "type": "str", "nullable": True}]}],
            "rewrites": [{"name": "price", "type": "float", "nullable": True}],
            "adds": [{"name": "note", "type": "str", "nullable": True}],
        }),
    ])
    resolved = workflow.find_workflow_stage("clean").output_schema
    assert [(c.name, c.type) for c in resolved.columns] == [
        ("price", "float"), ("title", "str"), ("note", "str")]


def test_starlark_consistent_extends_signature_accepted():
    stage = parse_stage(_starlark_row_function_stage(
        signature={
            "form": "extends",
            "reads": [{"input": "bills", "columns": [{"name": "price", "type": "str", "nullable": True}]}],
            "rewrites": [{"name": "price", "type": "float", "nullable": True}],
            "adds": [{"name": "note", "type": "str", "nullable": True}],
        },
    ))
    assert stage.signature is not None and stage.signature.form == "extends"


def test_starlark_replaces_form_rejected():
    msg = _issues(_starlark_row_function_stage(signature={
        "form": "replaces",
        "produces": _EDGE["columns"],
    }))
    assert "extends" in msg


# ── per-type cross-checks ────────────────────────────────────────────────────

def _llm_stage(*, reads):
    return {
        "id": "score",
        "description": "Score bills",
        "type": "llm_transform",
        "inputs": [{"id": "bills"}],
        "llm": {"prompt_data_template": "Price: {price}"},
        "signature": {
            "form": "extends",
            "reads": [{"input": "bills", "columns": reads}],
            "adds": [{"name": "score", "type": "int", "nullable": True}],
        },
    }


def test_llm_reads_must_match_template_placeholders_both_ways():
    msg = _issues(_llm_stage(reads=[
        {"name": "price", "type": "str", "nullable": True}, {"name": "title", "type": "str", "nullable": True},
    ]))
    assert "reads `title` but the prompt template never injects it" in msg

    msg = _issues(_llm_stage(reads=[{"name": "title", "type": "str", "nullable": True}]))
    assert "injects {price} but the signature does not read it" in msg


def test_llm_matching_reads_accepted():
    stage = parse_stage(_llm_stage(reads=[{"name": "price", "type": "str", "nullable": True}]))
    assert stage.signature is not None


def _join_stage(*, adds, reads=None, enrich_with=None):
    return {
        "id": "add_region",
        "description": "Add region",
        "type": "enrich",
        "inputs": [{"id": "bills"}, {"id": "states"}],
        "join": {"keys": [{"left": "state", "right": "code"}], "enrich_with": enrich_with or {"region": "region"}},
        "signature": {
            "form": "extends",
            "reads": reads if reads is not None else [
                {"input": "bills", "columns": [{"name": "state", "type": "str", "nullable": True}]},
                {"input": "states", "columns": [{"name": "code", "type": "str", "nullable": True}]},
            ],
            "adds": adds,
        },
    }


def test_join_key_must_be_read_from_its_side():
    msg = _join_issues(_join_stage(
        adds=[{"name": "region", "type": "str", "nullable": True}],
        reads=[{"input": "bills", "columns": [{"name": "state", "type": "str", "nullable": True}]}],
    ))
    assert "join key .right `code` is not read from the reference input" in msg


def test_join_add_must_be_landed():
    msg = _join_issues(_join_stage(
        adds=[{"name": "population", "type": "int", "nullable": True}],
    ))
    assert "population" in msg and "join.enrich_with does not land" in msg


def test_join_landed_column_must_be_added_by_the_signature():
    msg = _join_issues(_join_stage(adds=[]))
    assert "join.enrich_with lands `region` but the signature does not add it" in msg


def test_join_add_type_must_match_its_source():
    msg = _join_issues(_join_stage(
        adds=[{"name": "region", "type": "int", "nullable": True}],
    ))
    assert "its source `region` supplies" in msg


def test_join_signature_adds_the_landed_name_not_the_source():
    assert _join_issues(_join_stage(
        enrich_with={"region": "region_r"},
        adds=[{"name": "region_r", "type": "str", "nullable": True}],
    )) == ""


def test_join_consistent_signature_accepted():
    assert _join_issues(
        _join_stage(adds=[{"name": "region", "type": "str", "nullable": True}])) == ""


def test_aggregate_signature_must_tell_the_config_story():
    spec = {
        "id": "totals",
        "description": "Totals",
        "type": "aggregate",
        "inputs": [{"id": "facilities"}],
        "aggregate": {
            "group_by": ["company"],
            "aggregations": [
                {"output_column": "total", "formula": "sum", "value_column": "revenue"},
            ],
        },
        "signature": {
            "form": "replaces",
            "reads": [
                {
                    "input": "facilities",
                    "columns": [{"name": "company", "type": "str", "nullable": True}],
                },
            ],
            "produces": [{"name": "company", "type": "str", "nullable": True}],
        },
    }
    msg = _workflow_issues(spec, source_stage("facilities", [
        {"name": "company", "type": "str", "nullable": True},
        {"name": "revenue", "type": "int", "nullable": True},
    ]))
    assert "consumes `revenue` but the signature does not read it" in msg
    assert "emits `total` but the signature's produces omits it" in msg


def _union_spec(**signature):
    return {
        "id": "all_bills",
        "description": "All bills",
        "type": "union",
        "inputs": [{"id": "house"}, {"id": "senate"}],
        "union": {},
        "signature": {"form": "extends", "reads": [], "adds": [], "rewrites": [],
                      **signature},
    }


@pytest.mark.parametrize("field, entries", [
    ("reads", [{"input": "house",
                "columns": [{"name": "price", "type": "str", "nullable": True}]}]),
    ("adds", [{"name": "extra", "type": "str", "nullable": True}]),
    ("rewrites", [{"name": "price", "type": "str", "nullable": True}]),
])
def test_a_union_signature_declares_nothing(field, entries):
    """Its output IS the shared input schema, so anything written here is a second copy."""
    msg = _issues(_union_spec(**{field: entries}))
    assert "signature declares nothing" in msg and field in msg


def test_a_union_signature_that_declares_nothing_is_accepted():
    assert parse_stage(_union_spec()).signature.form == "extends"


def test_review_queue_add_outside_the_review_columns_rejected():
    spec = {
        "id": "check",
        "description": "Check",
        "type": "human_review_queue",
        "inputs": [{"id": "bills"}],
        "queue": {
            "reviewed_columns": {"price": "reviewed_price"},
            "verdict_column": "verdict",
            "reviewer_column": "reviewer",
            "reviewed_at_column": "reviewed_at",
        },
        "signature": {
            "form": "extends",
            "reads": [{"input": "bills", "columns": [
                {"name": "price", "type": "str", "nullable": True}]}],
            "adds": [{"name": "hunch", "type": "str", "nullable": True}],
        },
    }
    assert "adds `hunch`, which the review runtime never writes" in _workflow_issues(spec)


def test_report_signature_must_produce_nothing():
    spec = {
        "id": "report",
        "description": "Report",
        "type": "report",
        "inputs": [{"id": "bills"}],
        "report": {"format": "csv"},
        "function": {"kind": "inline", "code": "def transform(df, output_dir, citation_provider):\n    return df"},
        "signature": {
            "form": "replaces",
            "reads": [{"input": "bills", "columns": [{"name": "price", "type": "str", "nullable": True}]}],
            "produces": [{"name": "path", "type": "str", "nullable": True}],
        },
    }
    msg = _issues(spec)
    assert "report emits files, not a table" in msg


# ── the schemas a stage's TESTS are stated in ────────────────────────────────
# A test states what the transform consumes and what it writes — neither of which
# is the stage's own input/output, wherever a column merely flows past the stage.

def test_read_schemas_narrow_each_input_to_what_the_transform_consumes():
    stage = parse_stage(_row_function_stage(signature={
        "form": "extends",
        "reads": [{"input": "bills", "columns": [
            {"name": "price", "type": "str", "nullable": True}]}],
        "adds": [{"name": "note", "type": "str", "nullable": True}],
    }))
    assert [c.name for c in transform_input_schemas(stage)["bills"].columns] == ["price"]


def test_the_transform_output_is_what_it_writes_not_what_flows_past_it():
    spec = _row_function_stage(signature={
        "form": "extends",
        "reads": [{"input": "bills", "columns": [
            {"name": "price", "type": "str", "nullable": True}]}],
        "adds": [{"name": "note", "type": "str", "nullable": True}],
    })
    workflow = parse_workflow([source_stage("bills", _EDGE["columns"]), spec])
    # `price` and `title` are on the STAGE's output — one read, one not — because
    # both flow. Neither is written, so neither is the transform's output.
    placed = workflow.find_workflow_stage("clean")
    assert [c.name for c in placed.output_schema.columns] == ["price", "title", "note"]
    assert [c.name for c in transform_output_schema(placed.stage).columns] == ["note"]


def test_a_rewritten_column_is_written_so_it_is_on_the_transform_output():
    stage = parse_stage(_row_function_stage(signature={
        "form": "extends",
        "reads": [{"input": "bills", "columns": [
            {"name": "price", "type": "str", "nullable": True}]}],
        "rewrites": [{"name": "price", "type": "float", "nullable": True}],
    }))
    assert [(c.name, c.type) for c in transform_output_schema(stage).columns] == [
        ("price", "float")]


def test_a_replaces_form_states_its_whole_output_however_narrow_its_reads():
    stage = parse_stage({
        "id": "regroup",
        "description": "Regroup bills",
        "type": "python_frame_function",
        "inputs": [{"id": "bills"}],
        "function": {"kind": "inline", "code": "def transform(df):\n    return df"},
        "signature": {
            "form": "replaces",
            "reads": [{"input": "bills", "columns": [
                {"name": "price", "type": "str", "nullable": True}]}],
            "produces": [{"name": "total", "type": "float", "nullable": True}],
        },
    })
    # Nothing flows under `replaces`, so narrowing the input narrows no output.
    assert [c.name for c in transform_output_schema(stage).columns] == ["total"]
    assert [c.name for c in transform_input_schemas(stage)["bills"].columns] == ["price"]


def test_a_row_function_that_reads_nothing_is_accepted():
    # One that stamps a constant consumes no column, and that is honest.
    stage = parse_stage({
        "id": "stamp", "description": "Stamp", "type": "python_row_function",
        "inputs": [{"id": "load"}],
        "function": {"kind": "inline",
                     "code": "def transform(row):\n    return {'src': 'q1'}\n"},
        "signature": {"form": "extends",
                      "adds": [{"name": "src", "type": "str", "nullable": True}]},
    })
    assert stage.anchor_reads() == frozenset()

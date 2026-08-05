"""The transform signature: its own shape rules, the stage-level rules
(`find_signature_issues`), and each type's config-vs-signature cross-check.
A stage without a signature is untouched — that invariant carries every stage
stored before signatures existed."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.stage import parse_stage


_EDGE = {
    "columns": [
        {"name": "price", "type": "str", "nullable": True},
        {"name": "title", "type": "str", "nullable": True},
    ],
}


def _row_function_stage(*, signature=None, output_columns=None):
    """One python_row_function stage dict over a price/title input edge."""
    spec = {
        "id": "clean",
        "name": "Clean prices",
        "type": "python_row_function",
        "inputs": [{"id": "bills", "schema": _EDGE}],
        "function": {"kind": "inline", "code": "def transform(row):\n    return row"},
        "output_schema": {"columns": output_columns or _EDGE["columns"]},
    }
    if signature is not None:
        spec["signature"] = signature
    return spec


def _issues(stage_dict) -> str:
    with pytest.raises(ValidationError) as err:
        parse_stage(stage_dict)
    return str(err.value)


def _starlark_row_function_stage(*, signature=None, output_columns=None):
    """One starlark_row_function stage dict over a price/title input edge."""
    spec = {
        "id": "clean",
        "name": "Clean prices",
        "type": "starlark_row_function",
        "inputs": [{"id": "bills", "schema": _EDGE}],
        "starlark": {"code": "def transform(row):\n    return row"},
        "output_schema": {"columns": output_columns or _EDGE["columns"]},
    }
    if signature is not None:
        spec["signature"] = signature
    return spec


# ── shape rules on the signature itself ──────────────────────────────────────

def test_stage_without_signature_is_untouched():
    stage = parse_stage(_row_function_stage())
    assert stage.signature is None
    assert "signature" not in stage.model_dump(mode="json", by_alias=True, exclude_none=True)


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
    msg = _issues(_row_function_stage(signature={
        "form": "extends",
        "reads": [{"input": "nowhere", "columns": [{"name": "price", "type": "str", "nullable": True}]}],
    }))
    assert "nowhere" in msg and "not one of this stage's inputs" in msg


def test_read_column_the_edge_does_not_supply_rejected():
    msg = _issues(_row_function_stage(signature={
        "form": "extends",
        "reads": [{"input": "bills", "columns": [{"name": "amount", "type": "int", "nullable": True}]}],
    }))
    assert "'amount'" in msg and "absent" in msg


def test_read_column_with_a_differing_spec_rejected():
    msg = _issues(_row_function_stage(signature={
        "form": "extends",
        "reads": [{"input": "bills", "columns": [{"name": "price", "type": "float", "nullable": True}]}],
    }))
    assert "'price'" in msg and "differs" in msg


def test_rewrite_without_reading_the_column_rejected():
    msg = _issues(_row_function_stage(
        signature={
            "form": "extends",
            "rewrites": [{"name": "price", "type": "float", "nullable": True}],
        },
    ))
    assert "rewrites `price` without reading it" in msg


def test_add_colliding_with_an_anchor_column_rejected():
    msg = _issues(_row_function_stage(signature={
        "form": "extends",
        "adds": [{"name": "title", "type": "str", "nullable": True}],
    }))
    assert "adds `title`" in msg and "already supplies" in msg


def test_output_schema_must_match_the_extended_anchor():
    # The signature promises price rewritten to float + a new note column, but
    # the declared output keeps price as str and omits the note.
    msg = _issues(_row_function_stage(
        signature={
            "form": "extends",
            "reads": [{"input": "bills", "columns": [{"name": "price", "type": "str", "nullable": True}]}],
            "rewrites": [{"name": "price", "type": "float", "nullable": True}],
            "adds": [{"name": "note", "type": "str", "nullable": True}],
        },
    ))
    assert "output_schema disagrees" in msg


def test_consistent_extends_signature_accepted():
    stage = parse_stage(_row_function_stage(
        signature={
            "form": "extends",
            "reads": [{"input": "bills", "columns": [{"name": "price", "type": "str", "nullable": True}]}],
            "rewrites": [{"name": "price", "type": "float", "nullable": True}],
            "adds": [{"name": "note", "type": "str", "nullable": True}],
        },
        output_columns=[
            {"name": "price", "type": "float", "nullable": True},
            {"name": "title", "type": "str", "nullable": True},
            {"name": "note", "type": "str", "nullable": True},
        ],
    ))
    assert stage.signature is not None and stage.signature.form == "extends"


def test_signature_changes_the_fingerprint_and_absence_preserves_it():
    bare = parse_stage(_row_function_stage())
    signed = parse_stage(_row_function_stage(
        signature={
            "form": "extends",
            "reads": [{"input": "bills", "columns": [{"name": "price", "type": "str", "nullable": True}]}],
            "rewrites": [{"name": "price", "type": "float", "nullable": True}],
        },
        output_columns=[
            {"name": "price", "type": "float", "nullable": True},
            {"name": "title", "type": "str", "nullable": True},
        ],
    ))
    assert bare.compute_definition_fingerprint() != signed.compute_definition_fingerprint()
    # Absence dumps nothing, so a stage stored before signatures existed keeps
    # its fingerprint: the payload has no signature key at all.
    again = parse_stage(_row_function_stage())
    assert bare.compute_definition_fingerprint() == again.compute_definition_fingerprint()


def test_internal_namespace_refused_in_signature_columns():
    msg = _issues(_row_function_stage(signature={
        "form": "extends",
        "adds": [{"name": "_hidden", "type": "str", "nullable": True}],
    }))
    assert "_hidden" in msg and "reserved" in msg


# ── starlark_row_function mirrors python_row_function's extends-form rules ────

def test_starlark_stage_without_signature_is_untouched():
    stage = parse_stage(_starlark_row_function_stage())
    assert stage.signature is None
    assert "signature" not in stage.model_dump(mode="json", by_alias=True, exclude_none=True)


def test_starlark_add_colliding_with_an_anchor_column_rejected():
    msg = _issues(_starlark_row_function_stage(signature={
        "form": "extends",
        "adds": [{"name": "title", "type": "str", "nullable": True}],
    }))
    assert "adds `title`" in msg and "already supplies" in msg


def test_starlark_rewrite_without_reading_the_column_rejected():
    msg = _issues(_starlark_row_function_stage(signature={
        "form": "extends",
        "rewrites": [{"name": "price", "type": "float", "nullable": True}],
    }))
    assert "rewrites `price` without reading it" in msg


def test_starlark_output_schema_must_match_the_extended_anchor():
    msg = _issues(_starlark_row_function_stage(
        signature={
            "form": "extends",
            "reads": [{"input": "bills", "columns": [{"name": "price", "type": "str", "nullable": True}]}],
            "rewrites": [{"name": "price", "type": "float", "nullable": True}],
            "adds": [{"name": "note", "type": "str", "nullable": True}],
        },
    ))
    assert "output_schema disagrees" in msg


def test_starlark_consistent_extends_signature_accepted():
    stage = parse_stage(_starlark_row_function_stage(
        signature={
            "form": "extends",
            "reads": [{"input": "bills", "columns": [{"name": "price", "type": "str", "nullable": True}]}],
            "rewrites": [{"name": "price", "type": "float", "nullable": True}],
            "adds": [{"name": "note", "type": "str", "nullable": True}],
        },
        output_columns=[
            {"name": "price", "type": "float", "nullable": True},
            {"name": "title", "type": "str", "nullable": True},
            {"name": "note", "type": "str", "nullable": True},
        ],
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
        "name": "Score bills",
        "type": "llm_transform",
        "inputs": [{"id": "bills", "schema": {
            "columns": _EDGE["columns"],
        }}],
        "llm": {"prompt_data_template": "Price: {price}"},
        "signature": {
            "form": "extends",
            "reads": [{"input": "bills", "columns": reads}],
            "adds": [{"name": "score", "type": "int", "nullable": True}],
        },
        "output_schema": {
            "columns": [*_EDGE["columns"], {"name": "score", "type": "int", "nullable": True}],
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


def _join_stage(*, adds, reads=None, enrich_with=None, output_adds=None):
    subject = {"columns": [
        {"name": "state", "type": "str", "nullable": True}, {"name": "bill", "type": "str", "nullable": True},
    ]}
    reference = {"columns": [
        {"name": "code", "type": "str", "nullable": True}, {"name": "region", "type": "str", "nullable": True},
    ]}
    return {
        "id": "add_region",
        "name": "Add region",
        "type": "enrich",
        "inputs": [{"id": "bills", "schema": subject}, {"id": "states", "schema": reference}],
        "join": {"keys": [{"left": "state", "right": "code"}], "enrich_with": enrich_with or {"region": "region"}},
        "signature": {
            "form": "extends",
            "reads": reads if reads is not None else [
                {"input": "bills", "columns": [{"name": "state", "type": "str", "nullable": True}]},
                {"input": "states", "columns": [{"name": "code", "type": "str", "nullable": True}]},
            ],
            "adds": adds,
        },
        "output_schema": {"columns": [
            {"name": "state", "type": "str", "nullable": True}, {"name": "bill", "type": "str", "nullable": True},
            *[{"name": c["name"], "type": c["type"], "nullable": True}
              for c in (adds if output_adds is None else output_adds)],
        ]},
    }


def test_join_key_must_be_read_from_its_side():
    msg = _issues(_join_stage(
        adds=[{"name": "region", "type": "str", "nullable": True}],
        reads=[{"input": "bills", "columns": [{"name": "state", "type": "str", "nullable": True}]}],
    ))
    assert "join key .right `code` is not read from the reference input" in msg


def test_join_add_must_be_landed():
    # Output declares only subject columns, so the signature check speaks, not deliverability.
    msg = _issues(_join_stage(
        adds=[{"name": "population", "type": "int", "nullable": True}], output_adds=[],
    ))
    assert "population" in msg and "join.enrich_with does not land" in msg


def test_join_landed_column_must_be_added_by_the_signature():
    msg = _issues(_join_stage(adds=[]))
    assert "join.enrich_with lands `region` but the signature does not add it" in msg


def test_join_add_type_must_match_its_source():
    msg = _issues(_join_stage(
        adds=[{"name": "region", "type": "int", "nullable": True}], output_adds=[],
    ))
    assert "its source `region` supplies" in msg


def test_join_signature_adds_the_landed_name_not_the_source():
    stage = parse_stage(_join_stage(
        enrich_with={"region": "region_r"},
        adds=[{"name": "region_r", "type": "str", "nullable": True}],
    ))
    assert stage.signature is not None


def test_join_consistent_signature_accepted():
    stage = parse_stage(_join_stage(adds=[{"name": "region", "type": "str", "nullable": True}]))
    assert stage.signature is not None


def test_aggregate_signature_must_tell_the_config_story():
    spec = {
        "id": "totals",
        "name": "Totals",
        "type": "aggregate",
        "inputs": [{"id": "facilities", "schema": {"columns": [
            {"name": "company", "type": "str", "nullable": True}, {"name": "revenue", "type": "int", "nullable": True},
        ]}}],
        "aggregate": {
            "group_by": ["company"],
            "aggregations": [
                {"output_column": "total", "formula": "sum", "value_column": "revenue"},
            ],
        },
        "signature": {
            "form": "replaces",
            "reads": [{"input": "facilities", "columns": [{"name": "company", "type": "str", "nullable": True}]}],
            "produces": [{"name": "company", "type": "str", "nullable": True}],
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
    msg = _issues(spec)
    assert "consumes `revenue` but the signature does not read it" in msg
    assert "emits `total` but the signature's produces omits it" in msg


def test_union_signature_reads_nothing_and_produces_from_every_input():
    spec = {
        "id": "all_bills",
        "name": "All bills",
        "type": "union",
        "inputs": [{"id": "house", "schema": _EDGE}, {"id": "senate", "schema": _EDGE}],
        "union": {},
        "signature": {
            "form": "replaces",
            "reads": [{"input": "house", "columns": [{"name": "price", "type": "str", "nullable": True}]}],
            "produces": _EDGE["columns"],
        },
        "signature": {
            "form": "replaces",
            "reads": [
                {
                    "input": "house",
                    "columns": [{"name": "price", "type": "str", "nullable": True}],
                },
            ],
            "produces": _EDGE["columns"],
        },
    }
    msg = _issues(spec)
    assert "signature reads must be empty" in msg


def test_review_queue_add_outside_the_review_columns_rejected():
    spec = {
        "id": "check",
        "name": "Check",
        "type": "human_review_queue",
        "inputs": [{"id": "bills", "schema": _EDGE}],
        "queue": {
            "reviewed_columns": {"price": "reviewed_price"},
            "verdict_column": "verdict",
            "reviewer_column": "reviewer",
            "reviewed_at_column": "reviewed_at",
        },
        "signature": {
            "form": "extends",
            "adds": [{"name": "hunch", "type": "str", "nullable": True}],
        },
        "signature": {
            "form": "extends",
            "adds": [{"name": "hunch", "type": "str", "nullable": True}],
        },
    }
    msg = _issues(spec)
    assert "adds `hunch`, which the review runtime never writes" in msg


def test_publish_signature_must_produce_nothing():
    spec = {
        "id": "report",
        "name": "Report",
        "type": "publish",
        "inputs": [{"id": "bills", "schema": _EDGE}],
        "publish": {"format": "csv"},
        "function": {"kind": "inline", "code": "def transform(df, output_dir, trace_links):\n    return df"},
        "signature": {
            "form": "replaces",
            "reads": [{"input": "bills", "columns": [{"name": "price", "type": "str", "nullable": True}]}],
            "produces": [{"name": "path", "type": "str", "nullable": True}],
        },
    }
    msg = _issues(spec)
    assert "publish emits files, not a table" in msg

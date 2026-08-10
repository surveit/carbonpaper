"""build_schema_table_graph — the tier-1 table-level map of the data model.

Pins the honesty contract: edges come ONLY from declared column `references`
(the same deterministic source as the ER view), so the graph can never draw a
dataflow story the schemas don't state. See the builder's docstring."""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.models.named_schemas import NamedSchema
from app.web.diagrams import build_schema_table_graph


def _schema(**fields: Any) -> NamedSchema:
    return NamedSchema.model_validate({"kind": "reference", "title": "T", **fields})


def _column(name: str, **fields: Any) -> dict[str, Any]:
    return {"name": name, "type": "str", "nullable": True, **fields}


_SCHEMAS = [
    _schema(name="a", kind="reference", title="A lookup table", columns=[_column("id")]),
    _schema(name="b", kind="computed", columns=[
        _column("a_id", references="a"),
        _column("a_other", references="a.id"),
    ]),
]


def test_nodes_carry_name_title_and_kind_class():
    src = build_schema_table_graph(_SCHEMAS)
    assert src.startswith("flowchart LR")
    assert "<b>a</b>" in src and "A lookup table" in src
    assert ":::input" in src   # reference kind → the shared SCHEMA_KIND_CLASS
    assert ":::python" in src  # computed kind
    assert 'click a call focusSchema("a")' in src


def test_fk_edges_only_and_deduped_at_table_level():
    src = build_schema_table_graph(_SCHEMAS)
    # Two referencing columns in b, ONE table-level edge — and drawn from the
    # referenced table to the key-holder, never the reverse.
    assert src.count("a --> b") == 1
    assert "b --> a" not in src


def test_self_and_unresolved_references_draw_no_edge():
    schemas = [_schema(name="x", kind="computed", columns=[
        _column("x_id", references="x"),
        _column("z_id", references="zzz"),
    ])]
    assert "-->" not in build_schema_table_graph(schemas)


def test_title_identical_to_name_suppresses_the_subtitle_span():
    schemas = [_schema(name="a", kind="reference", title="a", columns=[_column("id")])]
    src = build_schema_table_graph(schemas)
    assert src.count("<b>a</b>") == 1
    assert "<span" not in src


def test_a_nameless_or_unknown_kind_schema_never_reaches_the_graph():
    """The model refuses the two cases the builder used to tolerate."""
    with pytest.raises(ValidationError):
        _schema(columns=[_column("id")])
    with pytest.raises(ValidationError):
        _schema(name="a", kind="weird", columns=[_column("id")])


def test_only_classdefs_when_the_data_model_is_empty():
    assert build_schema_table_graph([]) == "\n".join([
        "flowchart LR",
        "    classDef aggregate fill:#fdfdfe,stroke:#e9e9eb,color:#24272b",
        "    classDef custom fill:#fdfdfe,stroke:#e9e9eb,color:#24272b",
        "    classDef human fill:#fdfdfe,stroke:#e9e9eb,color:#24272b",
        "    classDef input fill:#fdfdfe,stroke:#e9e9eb,color:#24272b",
        "    classDef python fill:#fdfdfe,stroke:#e9e9eb,color:#24272b",
    ])


def test_no_fabricated_edges_without_references():
    """A table that reads another without storing its key (e.g. a roll-up) gets
    no edge — the graph under-claims rather than inventing dataflow."""
    schemas = [
        _schema(name="comment", kind="computed", columns=[_column("comment_id")]),
        _schema(name="coverage_summary", kind="computed",
                columns=[_column("comment_count", type="int")]),
    ]
    assert "-->" not in build_schema_table_graph(schemas)

"""build_schema_table_graph — the tier-1 table-level map of the data model.

Pins the honesty contract: edges come ONLY from declared column `references`
(the same deterministic source as the ER view), so the graph can never draw a
dataflow story the schemas don't state. See the builder's docstring."""
from __future__ import annotations

from app.web.diagrams import build_schema_table_graph

_SCHEMAS = [
    {
        "name": "a",
        "kind": "reference",
        "title": "A lookup table",
        "columns": [{"name": "id", "type": "str"}],
    },
    {
        "name": "b",
        "kind": "computed",
        "columns": [
            {"name": "a_id", "type": "str", "references": "a"},
            {"name": "a_other", "type": "str", "references": "a.id"},
        ],
    },
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
    schemas = [
        {
            "name": "x",
            "kind": "computed",
            "columns": [
                {"name": "x_id", "type": "str", "references": "x"},
                {"name": "z_id", "type": "str", "references": "zzz"},
            ],
        }
    ]
    src = build_schema_table_graph(schemas)
    assert "-->" not in src


def test_no_fabricated_edges_without_references():
    """A table that reads another without storing its key (e.g. a roll-up) gets
    no edge — the graph under-claims rather than inventing dataflow."""
    schemas = [
        {"name": "comment", "kind": "computed",
         "columns": [{"name": "comment_id", "type": "str"}]},
        {"name": "coverage_summary", "kind": "computed",
         "columns": [{"name": "comment_count", "type": "int"}]},
    ]
    src = build_schema_table_graph(schemas)
    assert "-->" not in src

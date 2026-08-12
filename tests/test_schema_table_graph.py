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
        "columns": [{"name": "id", "type": "str", "nullable": True}],
    },
    {
        "name": "b",
        "kind": "computed",
        "columns": [
            {"name": "a_id", "type": "str", "references": "a", "nullable": True},
            {"name": "a_other", "type": "str", "references": "a.id", "nullable": True},
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
                {"name": "x_id", "type": "str", "references": "x", "nullable": True},
                {"name": "z_id", "type": "str", "references": "zzz", "nullable": True},
            ],
        }
    ]
    src = build_schema_table_graph(schemas)
    assert "-->" not in src


def test_title_identical_to_name_suppresses_the_subtitle_span():
    schemas = [{"name": "a", "kind": "reference", "title": "a",
                "columns": [{"name": "id", "type": "str", "nullable": True}]}]
    src = build_schema_table_graph(schemas)
    assert src.count("<b>a</b>") == 1
    assert "<span" not in src


def test_schema_with_no_name_draws_no_node():
    schemas = [{"kind": "reference", "columns": [{"name": "id", "type": "str", "nullable": True}]}]
    src = build_schema_table_graph(schemas)
    assert src == "\n".join([
        "flowchart LR",
        "    classDef aggregate fill:#fdfdfe,stroke:#e9e9eb,color:#24272b",
        "    classDef custom fill:#fdfdfe,stroke:#e9e9eb,color:#24272b",
        "    classDef human fill:#fdfdfe,stroke:#e9e9eb,color:#24272b",
        "    classDef input fill:#fdfdfe,stroke:#e9e9eb,color:#24272b",
        "    classDef python fill:#fdfdfe,stroke:#e9e9eb,color:#24272b",
    ])


def test_unrecognized_kind_gets_the_custom_class():
    schemas = [{"name": "a", "kind": "weird", "columns": [{"name": "id", "type": "str", "nullable": True}]}]
    src = build_schema_table_graph(schemas)
    assert ":::custom" in src


def test_no_fabricated_edges_without_references():
    schemas = [
        {"name": "comment", "kind": "computed",
         "columns": [{"name": "comment_id", "type": "str", "nullable": True}]},
        {"name": "coverage_summary", "kind": "computed",
         "columns": [{"name": "comment_count", "type": "int", "nullable": True}]},
    ]
    src = build_schema_table_graph(schemas)
    assert "-->" not in src


def test_a_schema_with_no_kind_takes_no_node_class():
    schemas = [{"name": "issue_text", "title": "Issue text", "columns": []}]
    # `custom` is where an unrecognised kind lands — never where a missing one does.
    src = build_schema_table_graph(schemas)
    node_line = [ln for ln in src.splitlines() if ln.strip().startswith("issue_text[")][0]
    assert ":::" not in node_line

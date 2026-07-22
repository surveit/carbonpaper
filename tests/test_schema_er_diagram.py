"""Behavior tests for build_schema_er_diagram (app/web/diagrams.py) — the
Mermaid erDiagram rendered for the data-model's NAMED schemas.

build_schema_er_diagram had zero test coverage before this branch's
decomposition — confirmed by grep across tests/. These tests pin its
generated-text behavior through the public function only, so the suite
passes identically against the pre-decomposition single function: one entity
block per named schema (PK/FK markers, truncated+quote-escaped description
comments, the empty-column "any" placeholder row), FK edges deduped and
derived only from explicit column `references`, and schemas/columns missing
a `name` skipped.
"""
from __future__ import annotations

from app.web.diagrams import build_schema_er_diagram


def test_empty_schema_list_renders_bare_header():
    assert build_schema_er_diagram([]) == "erDiagram"


def test_entity_block_with_pk_and_plain_column():
    schemas = [{
        "name": "orgs",
        "columns": [
            {"name": "id", "type": "str"},
            {"name": "title", "type": "str"},
        ],
        "primary_key": ["id"],
    }]
    diagram = build_schema_er_diagram(schemas)
    assert diagram == (
        "erDiagram\n"
        "    orgs {\n"
        "        str id PK\n"
        "        str title\n"
        "    }"
    )


def test_column_description_truncated_to_48_chars_and_quotes_escaped():
    long_desc = "x" * 60
    schemas = [{
        "name": "orgs",
        "columns": [{"name": "title", "type": "str", "description": f'has "quotes" {long_desc}'}],
    }]
    diagram = build_schema_er_diagram(schemas)
    comment_line = [ln for ln in diagram.splitlines() if "title" in ln][0]
    assert comment_line == '        str title "has \'quotes\' ' + long_desc[: 48 - len("has 'quotes' ")] + '"'
    assert len(comment_line.split('"')[1]) == 48


def test_schema_with_no_columns_renders_any_placeholder_with_kind():
    schemas = [{"name": "empty_one", "kind": "reference", "columns": []}]
    diagram = build_schema_er_diagram(schemas)
    assert diagram == (
        "erDiagram\n"
        "    empty_one {\n"
        "        any _ \"(reference)\"\n"
        "    }"
    )


def test_column_type_sanitized_for_mermaid():
    schemas = [{"name": "orgs", "columns": [{"name": "tags", "type": "list[str]"}]}]
    diagram = build_schema_er_diagram(schemas)
    assert "list_str tags" in diagram


def test_schema_missing_name_is_skipped():
    schemas = [{"columns": [{"name": "id", "type": "str"}]}, {"name": "orgs", "columns": []}]
    diagram = build_schema_er_diagram(schemas)
    assert "orgs" in diagram
    assert diagram.count("{") == 1


def test_column_missing_name_is_skipped():
    schemas = [{"name": "orgs", "columns": [{"type": "str"}, {"name": "id", "type": "str"}]}]
    diagram = build_schema_er_diagram(schemas)
    assert diagram == (
        "erDiagram\n"
        "    orgs {\n"
        "        str id\n"
        "    }"
    )


def test_fk_edge_drawn_from_referenced_schema_to_referencing_schema():
    schemas = [
        {"name": "orgs", "columns": [{"name": "id", "type": "str"}], "primary_key": ["id"]},
        {"name": "filings", "columns": [
            {"name": "org_id", "type": "str", "references": "orgs.id"},
        ]},
    ]
    diagram = build_schema_er_diagram(schemas)
    assert "    orgs ||--o{ filings : org_id" in diagram
    fk_line = [ln for ln in diagram.splitlines() if "org_id" in ln and "FK" in ln][0]
    assert fk_line == "        str org_id FK"


def test_fk_edge_to_unknown_schema_is_dropped():
    schemas = [{"name": "filings", "columns": [
        {"name": "org_id", "type": "str", "references": "ghost.id"},
    ]}]
    diagram = build_schema_er_diagram(schemas)
    assert "||--o{" not in diagram


def test_self_referencing_column_draws_no_edge():
    schemas = [{"name": "orgs", "columns": [
        {"name": "parent_id", "type": "str", "references": "orgs.id"},
    ]}]
    diagram = build_schema_er_diagram(schemas)
    assert "||--o{" not in diagram


def test_duplicate_fk_edges_are_deduped():
    """Two columns that resolve to the identical edge line (same referencing
    schema, same target, same column name) collapse to one edge — the
    columns list is not deduped, so a literal repeat is the way to hit it."""
    schemas = [
        {"name": "orgs", "columns": [{"name": "id", "type": "str"}]},
        {"name": "filings", "columns": [
            {"name": "org_id", "type": "str", "references": "orgs.id"},
            {"name": "org_id", "type": "str", "references": "orgs.id"},
        ]},
    ]
    diagram = build_schema_er_diagram(schemas)
    assert diagram.count("orgs ||--o{ filings") == 1

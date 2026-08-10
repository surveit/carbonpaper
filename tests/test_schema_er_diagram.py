from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.models.named_schemas import NamedSchema
from app.web.diagrams import build_schema_er_diagram


def _schema(**fields: Any) -> NamedSchema:
    """A NamedSchema with the fields the diagram doesn't read already filled."""
    return NamedSchema.model_validate({"kind": "reference", "title": "T", **fields})


def _column(name: str, **fields: Any) -> dict[str, Any]:
    return {"name": name, "type": "str", "nullable": True, **fields}


def test_empty_schema_list_renders_bare_header():
    assert build_schema_er_diagram([]) == "erDiagram"


def test_entity_block_with_pk_and_plain_column():
    schemas = [_schema(
        name="orgs",
        primary_key=["id"],
        columns=[_column("id"), _column("title")],
    )]
    assert build_schema_er_diagram(schemas) == (
        "erDiagram\n"
        "    orgs {\n"
        "        str id PK\n"
        "        str title\n"
        "    }"
    )


def test_column_description_truncated_to_48_chars_and_quotes_escaped():
    long_desc = "x" * 60
    schemas = [_schema(
        name="orgs",
        columns=[_column("title", description=f'has "quotes" {long_desc}')],
    )]
    diagram = build_schema_er_diagram(schemas)
    comment_line = [ln for ln in diagram.splitlines() if "title" in ln][0]
    assert comment_line == '        str title "has \'quotes\' ' + long_desc[: 48 - len("has 'quotes' ")] + '"'
    assert len(comment_line.split('"')[1]) == 48


def test_schema_with_no_columns_renders_any_placeholder_with_kind():
    schemas = [_schema(name="empty_one", kind="reference", columns=[])]
    assert build_schema_er_diagram(schemas) == (
        "erDiagram\n"
        "    empty_one {\n"
        "        any _ \"(reference)\"\n"
        "    }"
    )


def test_column_type_sanitized_for_mermaid():
    schemas = [_schema(name="orgs", columns=[_column("tags", type="list[str]")])]
    assert "list_str tags" in build_schema_er_diagram(schemas)


def test_a_nameless_schema_or_column_never_reaches_the_diagram():
    """The model refuses what the builder used to skip."""
    with pytest.raises(ValidationError):
        NamedSchema.model_validate({"kind": "reference", "title": "T", "columns": []})
    with pytest.raises(ValidationError):
        _schema(name="orgs", columns=[{"type": "str"}])


def test_fk_edge_drawn_from_referenced_schema_to_referencing_schema():
    schemas = [
        _schema(name="orgs", columns=[_column("id")]),
        _schema(name="filings", columns=[_column("org_id", references="orgs.id")]),
    ]
    diagram = build_schema_er_diagram(schemas)
    assert "    orgs ||--o{ filings : org_id" in diagram
    fk_line = [ln for ln in diagram.splitlines() if "org_id" in ln and "FK" in ln][0]
    assert fk_line == "        str org_id FK"


def test_fk_edge_to_unknown_schema_is_dropped():
    schemas = [_schema(name="filings", columns=[_column("org_id", references="ghost.id")])]
    assert "||--o{" not in build_schema_er_diagram(schemas)


def test_self_referencing_column_draws_no_edge():
    schemas = [_schema(name="orgs", columns=[_column("parent_id", references="orgs.id")])]
    assert "||--o{" not in build_schema_er_diagram(schemas)


def test_two_columns_onto_one_target_draw_one_edge_each():
    """Column names are unique, so two keys onto one target never collide."""
    schemas = [
        _schema(name="orgs", columns=[_column("id")]),
        _schema(name="filings", columns=[
            _column("org_id", references="orgs.id"),
            _column("parent_org_id", references="orgs.id"),
        ]),
    ]
    diagram = build_schema_er_diagram(schemas)
    assert diagram.count("orgs ||--o{ filings") == 2

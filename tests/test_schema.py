"""Tests for app/models/schema.py — named schemas (the data model)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app import models as m


def test_named_schema_valid():
    s = m.NamedSchema.model_validate(
        {"name": "company", "kind": "reference",
         "columns": [{"name": "company_id", "type": "str"}], "primary_key": ["company_id"]}
    )
    assert s.kind is m.SchemaKind.reference


def test_named_schema_bad_kind():
    with pytest.raises(ValidationError):
        m.NamedSchema.model_validate({"name": "x", "kind": "bogus", "columns": []})


def test_named_schema_name_snake_case():
    with pytest.raises(ValidationError):
        m.NamedSchema.model_validate({"name": "BadName", "kind": "input", "columns": []})


def test_named_column_carries_reference():
    s = m.NamedSchema.model_validate(
        {"name": "cell", "kind": "computed",
         "columns": [{"name": "company_id", "type": "str", "references": "company.company_id"}]}
    )
    assert s.columns[0].references == "company.company_id"


def test_exclusive_arc_column_must_be_declared():
    with pytest.raises(ValidationError):
        m.NamedSchema.model_validate(
            {"name": "cell", "kind": "computed",
             "columns": [{"name": "a", "nullable": True}], "exclusive_arcs": [["a", "b"]]}
        )


def test_exclusive_arc_column_must_be_nullable():
    with pytest.raises(ValidationError):
        m.NamedSchema.model_validate(
            {"name": "cell", "kind": "computed",
             "columns": [{"name": "a", "nullable": True}, {"name": "b", "nullable": False}],
             "exclusive_arcs": [["a", "b"]]}
        )


def test_exclusive_arc_ok():
    m.NamedSchema.model_validate(
        {"name": "cell", "kind": "computed",
         "columns": [{"name": "a", "nullable": True}, {"name": "b", "nullable": True}],
         "exclusive_arcs": [["a", "b"]]}
    )


# ── library ──────────────────────────────────────────────────────────────────
def test_library_unique_names():
    with pytest.raises(ValidationError):
        m.parse_schema_library([
            {"name": "x", "kind": "input", "columns": []},
            {"name": "x", "kind": "input", "columns": []},
        ])


def test_library_references_resolve():
    lib = m.parse_schema_library([
        {"name": "company", "kind": "reference",
         "columns": [{"name": "company_id", "type": "str"}], "primary_key": ["company_id"]},
        {"name": "cell", "kind": "computed",
         "columns": [{"name": "company_id", "type": "str", "references": "company.company_id"}]},
    ])
    assert [s.name for s in lib.schemas] == ["company", "cell"]


def test_library_dangling_reference():
    with pytest.raises(ValidationError):
        m.parse_schema_library([
            {"name": "cell", "kind": "computed", "columns": [{"name": "cid", "references": "ghost"}]},
        ])


def test_library_reference_unknown_column():
    with pytest.raises(ValidationError):
        m.parse_schema_library([
            {"name": "company", "kind": "reference", "columns": [{"name": "company_id", "type": "str"}]},
            {"name": "cell", "kind": "computed", "columns": [{"name": "cid", "references": "company.missing"}]},
        ])


def test_validate_schema_library_nonfatal():
    assert m.validate_schema_library(
        [{"name": "company", "kind": "reference", "columns": [{"name": "id", "type": "str"}]}]
    ) == []
    assert m.validate_schema_library(
        [{"name": "cell", "kind": "computed", "columns": [{"name": "cid", "references": "ghost"}]}]
    )

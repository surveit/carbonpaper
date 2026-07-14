"""Tests for app/core/models/named_schemas.py — the named data model."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core import models as m


def test_named_schema_valid():
    s = m.NamedSchema.model_validate(
        {"name": "company", "kind": "reference", "title": "Company",
         "columns": [{"name": "company_id", "type": "str"}], "primary_key": ["company_id"]}
    )
    assert s.kind == m.SchemaKind.reference


def test_named_schema_bad_kind():
    with pytest.raises(ValidationError):
        m.NamedSchema.model_validate({"name": "x", "kind": "bogus", "title": "X", "columns": []})


def test_named_schema_name_snake_case():
    with pytest.raises(ValidationError):
        m.NamedSchema.model_validate({"name": "BadName", "kind": "input", "title": "Bad", "columns": []})


def test_named_schema_title_required():
    with pytest.raises(ValidationError):
        m.NamedSchema.model_validate({"name": "company", "kind": "reference", "columns": []})


def test_named_schema_is_a_table_schema():
    """NamedSchema builds on the on-the-fly TableSchema; it inherits its column /
    primary-key checks rather than re-implementing them."""
    s = m.NamedSchema.model_validate(
        {"name": "company", "kind": "reference", "title": "Company",
         "columns": [{"name": "company_id", "type": "str"}]}
    )
    assert isinstance(s, m.TableSchema)
    with pytest.raises(ValidationError):   # inherited duplicate-column check
        m.NamedSchema.model_validate(
            {"name": "cell", "kind": "computed", "title": "Cell",
             "columns": [{"name": "a"}, {"name": "a"}]}
        )


def test_named_schema_source_is_source_ref():
    s = m.NamedSchema.model_validate(
        {"name": "company", "kind": "reference", "title": "Company",
         "columns": [{"name": "id", "type": "str"}],
         "source": {"doc": "methodology.md", "section": "Data model"}}
    )
    assert isinstance(s.source, m.SourceRef)
    assert s.source.doc == "methodology.md"


def test_named_column_carries_reference():
    s = m.NamedSchema.model_validate(
        {"name": "cell", "kind": "computed", "title": "Cell",
         "columns": [{"name": "company_id", "type": "str", "references": "company.company_id"}]}
    )
    assert s.columns[0].references == "company.company_id"


# ── library ──────────────────────────────────────────────────────────────────
def test_library_unique_names():
    with pytest.raises(ValidationError):
        m.parse_schema_library([
            {"name": "x", "kind": "input", "title": "X", "columns": []},
            {"name": "x", "kind": "input", "title": "X (dupe)", "columns": []},
        ])


def test_library_references_resolve():
    lib = m.parse_schema_library([
        {"name": "company", "kind": "reference", "title": "Company",
         "columns": [{"name": "company_id", "type": "str"}], "primary_key": ["company_id"]},
        {"name": "cell", "kind": "computed", "title": "Cell",
         "columns": [{"name": "company_id", "type": "str", "references": "company.company_id"}]},
    ])
    assert [s.name for s in lib.schemas] == ["company", "cell"]


def test_library_dangling_reference():
    with pytest.raises(ValidationError):
        m.parse_schema_library([
            {"name": "cell", "kind": "computed", "title": "Cell",
             "columns": [{"name": "cid", "references": "ghost"}]},
        ])


def test_library_reference_unknown_column():
    with pytest.raises(ValidationError):
        m.parse_schema_library([
            {"name": "company", "kind": "reference", "title": "Company",
             "columns": [{"name": "company_id", "type": "str"}]},
            {"name": "cell", "kind": "computed", "title": "Cell",
             "columns": [{"name": "cid", "references": "company.missing"}]},
        ])


def test_validate_schema_library_nonfatal():
    assert m.validate_schema_library(
        [{"name": "company", "kind": "reference", "title": "Company",
          "columns": [{"name": "id", "type": "str"}]}]
    ) == []
    assert m.validate_schema_library(
        [{"name": "cell", "kind": "computed", "title": "Cell",
          "columns": [{"name": "cid", "references": "ghost"}]}]
    )

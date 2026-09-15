"""Tests for app/models/named_schemas.py — the named data model."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app import models as m


def test_named_schema_valid():
    s = m.NamedSchema.model_validate(
        {"name": "company", "kind": "reference", "title": "Company",
         "columns": [{"name": "company_id", "type": "str", "nullable": True}]}
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
    s = m.NamedSchema.model_validate(
        {"name": "company", "kind": "reference", "title": "Company",
         "columns": [{"name": "company_id", "type": "str", "nullable": True}]}
    )
    assert isinstance(s, m.TableSchema)
    with pytest.raises(ValidationError):   # inherited duplicate-column check
        m.NamedSchema.model_validate(
            {"name": "cell", "kind": "computed", "title": "Cell",
             "columns": [{"name": "a", "type": "str", "nullable": True}, {"name": "a", "type": "str", "nullable": True}]}
        )


def test_named_schema_source_is_source_ref():
    s = m.NamedSchema.model_validate(
        {"name": "company", "kind": "reference", "title": "Company",
         "columns": [{"name": "id", "type": "str", "nullable": True}],
         "source": {"doc": "methodology.md", "section": "Data model"}}
    )
    assert isinstance(s.source, m.SourceRef)
    assert s.source.doc == "methodology.md"


def test_named_column_carries_reference():
    s = m.NamedSchema.model_validate(
        {"name": "cell", "kind": "computed", "title": "Cell",
         "columns": [{"name": "company_id", "type": "str", "references": "company.company_id", "nullable": True}]}
    )
    assert s.columns[0].references == "company.company_id"


def test_named_schema_names_the_row_type_one_of_its_rows_is():
    s = m.NamedSchema.model_validate(
        {"name": "company_filings", "kind": "input", "title": "Company filings",
         "row_type_id": "company", "columns": []}
    )
    assert s.row_type_id == "company"


def test_a_schema_naming_no_row_type_is_valid():
    # Whether it resolves is Terms', which holds both halves — a library holds one.
    assert m.NamedSchema.model_validate(
        {"name": "scratch", "title": "Scratch", "columns": []}
    ).row_type_id is None


def test_a_row_type_id_is_not_checked_against_anything_a_library_holds():
    lib = m.parse_schema_library(
        [{"name": "cell", "kind": "computed", "title": "Cell",
          "row_type_id": "nothing_declares_this", "columns": []}]
    )
    assert lib.schemas[0].row_type_id == "nothing_declares_this"


def test_a_schema_no_longer_carries_its_own_spellings():
    with pytest.raises(ValidationError):
        m.NamedSchema.model_validate(
            {"name": "firm", "title": "Firm", "also_written": ["registrant"], "columns": []}
        )


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
         "columns": [{"name": "company_id", "type": "str", "nullable": True}]},
        {"name": "cell", "kind": "computed", "title": "Cell",
         "columns": [{"name": "company_id", "type": "str", "references": "company.company_id", "nullable": True}]},
    ])
    assert [s.name for s in lib.schemas] == ["company", "cell"]


def test_library_dangling_reference():
    with pytest.raises(ValidationError):
        m.parse_schema_library([
            {"name": "cell", "kind": "computed", "title": "Cell",
             "columns": [{"name": "cid", "references": "ghost", "type": "str", "nullable": True}]},
        ])


def test_library_reference_unknown_column():
    with pytest.raises(ValidationError):
        m.parse_schema_library([
            {"name": "company", "kind": "reference", "title": "Company",
             "columns": [{"name": "company_id", "type": "str", "nullable": True}]},
            {"name": "cell", "kind": "computed", "title": "Cell",
             "columns": [{"name": "cid", "references": "company.missing", "type": "str", "nullable": True}]},
        ])


def test_validate_schema_library_nonfatal():
    assert m.validate_schema_library(
        [{"name": "company", "kind": "reference", "title": "Company",
          "columns": [{"name": "id", "type": "str", "nullable": True}]}]
    ) == []
    assert m.validate_schema_library(
        [{"name": "cell", "kind": "computed", "title": "Cell",
          "columns": [{"name": "cid", "references": "ghost", "type": "str", "nullable": True}]}]
    )


def test_primary_key_must_name_declared_columns():
    with pytest.raises(ValidationError, match="primary_key"):
        m.NamedSchema.model_validate({
            "name": "orgs", "kind": "input", "title": "Orgs",
            "columns": [{"name": "id", "type": "str", "nullable": True}],
            "primary_key": ["missing"],
        })

"""Tests for the schema capabilities in app/core/models/schema.py: Column.enum, the
recursive json/list[json] shape (Column.fields / Column.value_type),
TableSchema.subtract (strict=True and strict=False) / is_subset_of, and
TableSchema.to_prompt."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core import models as m


# ── Column.enum ──────────────────────────────────────────────────────────────
def test_enum_valid_on_str_column():
    c = m.Column.model_validate({"name": "status", "type": "str", "enum": ["a", "b"]})
    assert c.enum == ["a", "b"]


def test_enum_empty_list_rejected():
    with pytest.raises(ValidationError):
        m.Column.model_validate({"name": "status", "type": "str", "enum": []})


def test_enum_on_non_str_type_rejected():
    with pytest.raises(ValidationError):
        m.Column.model_validate({"name": "count", "type": "int", "enum": ["a", "b"]})


# ── dict type rejected ───────────────────────────────────────────────────────
def test_dict_type_rejected():
    with pytest.raises(ValidationError):
        m.Column.model_validate({"name": "payload", "type": "dict"})


# ── Column recursive json shape ──────────────────────────────────────────────
def test_json_with_fields_object():
    c = m.Column.model_validate({
        "name": "payload", "type": "json",
        "fields": [
            {"name": "x", "type": "str", "nullable": False},
            {"name": "y", "type": "int", "nullable": True},
        ],
    })
    assert c.value_type is None
    assert [f.name for f in c.fields] == ["x", "y"]


def test_json_with_nested_fields():
    c = m.Column.model_validate({
        "name": "payload", "type": "json",
        "fields": [
            {"name": "inner", "type": "json", "fields": [
                {"name": "z", "type": "str", "nullable": False},
            ]},
        ],
    })
    inner = c.fields[0]
    assert inner.type == "json"
    assert inner.fields[0].name == "z"


def test_list_json_with_fields_array_of_records():
    c = m.Column.model_validate({
        "name": "records", "type": "list[json]",
        "fields": [
            {"name": "topic", "type": "str", "nullable": False},
            {"name": "score", "type": "int", "nullable": True},
        ],
    })
    assert c.type == "list[json]"
    assert [f.name for f in c.fields] == ["topic", "score"]


def test_json_with_value_type_open_map():
    c = m.Column.model_validate({
        "name": "url_map", "type": "json", "value_type": "str",
    })
    assert c.value_type == "str"
    assert c.fields is None


def test_list_json_with_value_type_open_map():
    c = m.Column.model_validate({
        "name": "url_maps", "type": "list[json]", "value_type": "str",
    })
    assert c.value_type == "str"


def test_json_value_type_must_be_scalar():
    with pytest.raises(ValidationError):
        m.Column.model_validate({"name": "payload", "type": "json", "value_type": "json"})


def test_json_neither_fields_nor_value_type_rejected():
    with pytest.raises(ValidationError):
        m.Column.model_validate({"name": "payload", "type": "json"})


def test_json_both_fields_and_value_type_rejected():
    with pytest.raises(ValidationError):
        m.Column.model_validate({
            "name": "payload", "type": "json",
            "fields": [{"name": "x", "type": "str"}],
            "value_type": "str",
        })


def test_fields_forbidden_on_non_json_type():
    with pytest.raises(ValidationError):
        m.Column.model_validate({
            "name": "count", "type": "int",
            "fields": [{"name": "x", "type": "str"}],
        })


def test_value_type_forbidden_on_non_json_type():
    with pytest.raises(ValidationError):
        m.Column.model_validate({"name": "count", "type": "int", "value_type": "str"})


# ── TableSchema.subtract ─────────────────────────────────────────────────────
def _ts(**kwargs):
    return m.TableSchema.model_validate(kwargs)


def test_subtract_difference():
    a = _ts(columns=[{"name": "id", "type": "str"}, {"name": "name", "type": "str"}])
    b = _ts(columns=[
        {"name": "id", "type": "str"},
        {"name": "name", "type": "str"},
        {"name": "score", "type": "int"},
    ])
    diff = b.subtract(a)
    assert [c.name for c in diff.columns] == ["score"]


def test_subtract_result_has_no_primary_key_or_metadata():
    a = _ts(columns=[{"name": "id", "type": "str"}], primary_key=["id"])
    b = _ts(
        columns=[{"name": "id", "type": "str"}, {"name": "score", "type": "int"}],
        primary_key=["id"], estimated_rows=10, notes="some notes",
    )
    diff = b.subtract(a)
    assert diff.primary_key is None
    assert diff.estimated_rows is None
    assert diff.notes is None


def test_subtract_identical_spec_shared_column_is_fine():
    a = _ts(columns=[{"name": "id", "type": "str", "nullable": False}])
    b = _ts(columns=[
        {"name": "id", "type": "str", "nullable": False},
        {"name": "score", "type": "int"},
    ])
    diff = b.subtract(a)
    assert [c.name for c in diff.columns] == ["score"]


def test_subtract_description_only_difference_does_not_throw():
    a = _ts(columns=[{"name": "id", "type": "str", "description": "from producer"}])
    b = _ts(columns=[
        {"name": "id", "type": "str", "description": "from consumer"},
        {"name": "score", "type": "int"},
    ])
    diff = b.subtract(a)
    assert [c.name for c in diff.columns] == ["score"]


def test_subtract_source_only_difference_does_not_throw():
    a = _ts(columns=[{"name": "id", "type": "str", "source": "stage_a"}])
    b = _ts(columns=[
        {"name": "id", "type": "str", "source": "stage_b"},
        {"name": "score", "type": "int"},
    ])
    diff = b.subtract(a)
    assert [c.name for c in diff.columns] == ["score"]


def test_subtract_type_mismatch_throws():
    a = _ts(columns=[{"name": "id", "type": "str"}])
    b = _ts(columns=[{"name": "id", "type": "int"}])
    with pytest.raises(ValueError, match="id"):
        b.subtract(a)


def test_subtract_nullable_mismatch_throws():
    a = _ts(columns=[{"name": "id", "type": "str", "nullable": True}])
    b = _ts(columns=[{"name": "id", "type": "str", "nullable": False}])
    with pytest.raises(ValueError, match="id"):
        b.subtract(a)


def test_subtract_enum_mismatch_throws():
    a = _ts(columns=[{"name": "status", "type": "str", "enum": ["a", "b"]}])
    b = _ts(columns=[{"name": "status", "type": "str", "enum": ["a", "c"]}])
    with pytest.raises(ValueError, match="status"):
        b.subtract(a)


def test_subtract_range_mismatch_throws():
    a = _ts(columns=[{"name": "score", "type": "int", "range": [0, 10]}])
    b = _ts(columns=[{"name": "score", "type": "int", "range": [0, 100]}])
    with pytest.raises(ValueError, match="score"):
        b.subtract(a)


def test_subtract_fields_identical_passes():
    fields = [{"name": "x", "type": "str", "nullable": False}]
    a = _ts(columns=[{"name": "payload", "type": "json", "fields": fields}])
    b = _ts(columns=[
        {"name": "payload", "type": "json", "fields": fields},
        {"name": "score", "type": "int"},
    ])
    diff = b.subtract(a)
    assert [c.name for c in diff.columns] == ["score"]


def test_subtract_fields_differing_throws():
    a = _ts(columns=[{
        "name": "payload", "type": "json",
        "fields": [{"name": "x", "type": "str", "nullable": False}],
    }])
    b = _ts(columns=[{
        "name": "payload", "type": "json",
        "fields": [{"name": "x", "type": "int", "nullable": False}],
    }])
    with pytest.raises(ValueError, match="payload"):
        b.subtract(a)


def test_subtract_value_type_mismatch_throws():
    a = _ts(columns=[{"name": "url_map", "type": "json", "value_type": "str"}])
    b = _ts(columns=[{"name": "url_map", "type": "json", "value_type": "int"}])
    with pytest.raises(ValueError, match="url_map"):
        b.subtract(a)


def test_subtract_nested_field_prose_difference_does_not_throw():
    """Spec-equality recurses into `fields` but ignores prose at every level:
    a sub-column differing only in description/source is a producer-vs-consumer
    copy, not a spec change, so subtract must not throw."""
    a = _ts(columns=[{
        "name": "payload", "type": "json",
        "fields": [{"name": "x", "type": "str", "nullable": False, "description": "from producer"}],
    }])
    b = _ts(columns=[
        {"name": "payload", "type": "json",
         "fields": [{"name": "x", "type": "str", "nullable": False, "description": "from consumer"}]},
        {"name": "score", "type": "int"},
    ])
    diff = b.subtract(a)
    assert [c.name for c in diff.columns] == ["score"]


def test_subtract_nested_field_spec_difference_throws():
    """Recursion still catches a real spec change in a sub-column (nullable)."""
    a = _ts(columns=[{
        "name": "payload", "type": "json",
        "fields": [{"name": "x", "type": "str", "nullable": True}],
    }])
    b = _ts(columns=[{
        "name": "payload", "type": "json",
        "fields": [{"name": "x", "type": "str", "nullable": False}],
    }])
    with pytest.raises(ValueError, match="payload"):
        b.subtract(a)


def test_spec_column_fields_derived_from_model():
    """The fields subtract compares for spec-equality are DERIVED from the
    Column model (every field except the prose ones), so a newly added schema
    capability is compared automatically instead of being silently ignored."""
    from app.core.models import schema as sch
    prose = {"name", "description", "source"}
    assert set(sch._SPEC_COLUMN_FIELDS) == set(m.Column.model_fields) - prose


# ── TableSchema.column_for_name ───────────────────────────────────────────────
def test_column_for_name_finds_by_name_or_returns_none():
    a = _ts(columns=[{"name": "id", "type": "str"}, {"name": "score", "type": "int"}])
    col = a.column_for_name("score")
    assert col is not None
    assert col.name == "score"
    assert col.type == "int"
    assert a.column_for_name("gone") is None


# ── TableSchema.is_subset_of ─────────────────────────────────────────────────
def test_is_subset_of_true_when_present_and_identical():
    a = _ts(columns=[{"name": "id", "type": "str"}])
    b = _ts(columns=[{"name": "id", "type": "str"}, {"name": "score", "type": "int"}])
    assert a.is_subset_of(b) is True


def test_is_subset_of_false_when_column_absent():
    a = _ts(columns=[{"name": "id", "type": "str"}, {"name": "gone", "type": "str"}])
    b = _ts(columns=[{"name": "id", "type": "str"}])
    assert a.is_subset_of(b) is False


def test_is_subset_of_false_when_spec_differs():
    a = _ts(columns=[{"name": "id", "type": "str"}])
    b = _ts(columns=[{"name": "id", "type": "int"}])
    assert a.is_subset_of(b) is False


def test_is_subset_of_ignores_prose():
    a = _ts(columns=[{"name": "id", "type": "str", "description": "producer"}])
    b = _ts(columns=[{"name": "id", "type": "str", "description": "consumer"}])
    assert a.is_subset_of(b) is True


# ── TableSchema.subtract(strict=False) ───────────────────────────────────────
def test_subtract_strict_false_empty_when_subset():
    a = _ts(columns=[{"name": "id", "type": "str"}])
    b = _ts(columns=[{"name": "id", "type": "str"}, {"name": "score", "type": "int"}])
    assert a.subtract(b, strict=False).columns == []
    assert a.is_subset_of(b) is True


def test_subtract_strict_false_lists_absent_column():
    a = _ts(columns=[{"name": "id", "type": "str"}, {"name": "gone", "type": "str"}])
    b = _ts(columns=[{"name": "id", "type": "str"}])
    missing = a.subtract(b, strict=False).columns
    assert [c.name for c in missing] == ["gone"]
    assert a.is_subset_of(b) is False


def test_subtract_strict_false_lists_column_with_differing_spec():
    a = _ts(columns=[{"name": "id", "type": "str"}])
    b = _ts(columns=[{"name": "id", "type": "int"}])
    missing = a.subtract(b, strict=False).columns
    assert [c.name for c in missing] == ["id"]
    assert a.is_subset_of(b) is False


def test_subtract_strict_false_ignores_prose_differences():
    a = _ts(columns=[{"name": "id", "type": "str", "description": "producer"}])
    b = _ts(columns=[{"name": "id", "type": "str", "description": "consumer"}])
    assert a.subtract(b, strict=False).columns == []


def test_subtract_strict_false_does_not_throw_on_spec_delta():
    # Unlike the strict=True default, strict=False tolerates `other` NOT
    # being a spec-preserving subset of `self` -- it just reports what's
    # uncovered instead of raising.
    a = _ts(columns=[{"name": "id", "type": "str"}])
    b = _ts(columns=[{"name": "id", "type": "int"}])
    diff = a.subtract(b, strict=False)
    assert [c.name for c in diff.columns] == ["id"]


# ── TableSchema.to_prompt ────────────────────────────────────────────────────
def test_to_prompt_header_and_footer():
    ts = _ts(columns=[{"name": "id", "type": "str", "nullable": False}])
    prompt = ts.to_prompt()
    lines = prompt.splitlines()
    assert lines[0] == (
        "Return ONE JSON object only — no prose, no code fences — "
        "with exactly these keys:"
    )
    assert lines[-1] == "Any other key is invalid."


def test_to_prompt_required_never_null():
    ts = _ts(columns=[{"name": "id", "type": "str", "nullable": False}])
    prompt = ts.to_prompt()
    assert '"id"' in prompt
    assert "required, never null" in prompt


def test_to_prompt_or_null():
    ts = _ts(columns=[{"name": "note", "type": "str", "nullable": True}])
    prompt = ts.to_prompt()
    assert '"note"' in prompt
    assert "or null" in prompt


def test_to_prompt_type_wordings():
    ts = _ts(columns=[
        {"name": "a", "type": "str", "nullable": False},
        {"name": "b", "type": "int", "nullable": False},
        {"name": "c", "type": "float", "nullable": False},
        {"name": "d", "type": "bool", "nullable": False},
        {"name": "e", "type": "date", "nullable": False},
        {"name": "f", "type": "datetime", "nullable": False},
        {"name": "g", "type": "list[str]", "nullable": False},
    ])
    prompt = ts.to_prompt()
    assert "string" in prompt
    assert "integer" in prompt
    assert "number" in prompt
    assert "boolean" in prompt
    assert "ISO date string" in prompt
    assert "ISO datetime string" in prompt
    assert "array of string values" in prompt


def test_to_prompt_description_appended():
    ts = _ts(columns=[
        {"name": "id", "type": "str", "nullable": False, "description": "the row key"},
    ])
    prompt = ts.to_prompt()
    assert "the row key" in prompt


def test_to_prompt_enum_listed():
    ts = _ts(columns=[
        {"name": "status", "type": "str", "nullable": False, "enum": ["open", "closed"]},
    ])
    prompt = ts.to_prompt()
    assert "one of: open | closed" in prompt


def test_to_prompt_range_listed():
    ts = _ts(columns=[
        {"name": "score", "type": "int", "nullable": False, "range": [0, 100]},
    ])
    prompt = ts.to_prompt()
    assert "between 0 and 100 inclusive" in prompt


def test_range_on_str_column_rejected_use_enum():
    """`range` is numeric bounds only; a categorical string vocabulary must be
    declared with `enum`, not the old `range: [val1, val2, ...]` convention."""
    with pytest.raises(ValidationError, match="enum"):
        m.Column(name="source_class", type="str",
                 range=["org_websites", "corporate_media", "CDP"])


def test_numeric_range_on_str_column_rejected():
    """Even an all-numeric range is invalid on a non-numeric column."""
    with pytest.raises(ValidationError, match="range"):
        m.Column(name="code", type="str", range=[0, 10])


def test_range_must_be_exactly_two_numbers():
    with pytest.raises(ValidationError, match="range"):
        m.Column(name="score", type="int", range=[0])


def test_range_numeric_pair_on_numeric_column_valid():
    col = m.Column(name="score", type="int", range=[0, 100])
    assert col.range == [0, 100]


def test_to_prompt_json_fields_renders_nested_object():
    ts = _ts(columns=[{
        "name": "payload", "type": "json", "nullable": False,
        "fields": [
            {"name": "x", "type": "str", "nullable": False},
            {"name": "y", "type": "int", "nullable": True},
        ],
    }])
    prompt = ts.to_prompt()
    assert '"payload": an object with keys:' in prompt
    assert '"x": string (required, never null)' in prompt
    assert '"y": integer (or null)' in prompt


def test_to_prompt_list_json_fields_renders_array_of_records():
    ts = _ts(columns=[{
        "name": "records", "type": "list[json]", "nullable": False,
        "fields": [
            {"name": "topic", "type": "str", "nullable": False},
            {"name": "explanation", "type": "str", "nullable": False},
        ],
    }])
    prompt = ts.to_prompt()
    assert '"records": an array of objects, each with keys:' in prompt
    assert '"topic": string (required, never null)' in prompt
    assert '"explanation": string (required, never null)' in prompt


def test_to_prompt_json_value_type_open_map_wording():
    ts = _ts(columns=[{
        "name": "url_map", "type": "json", "nullable": False, "value_type": "str",
    }])
    prompt = ts.to_prompt()
    assert '"url_map": an object mapping string keys to string values (required, never null)' in prompt


def test_to_prompt_list_json_value_type_open_map_wording():
    ts = _ts(columns=[{
        "name": "url_maps", "type": "list[json]", "nullable": False, "value_type": "str",
    }])
    prompt = ts.to_prompt()
    assert (
        '"url_maps": an array of objects, each mapping string keys to string '
        'values (required, never null)' in prompt
    )


def test_to_prompt_nested_json_fields_recurse():
    ts = _ts(columns=[{
        "name": "payload", "type": "json", "nullable": False,
        "fields": [
            {"name": "inner", "type": "json", "nullable": False, "fields": [
                {"name": "z", "type": "bool", "nullable": False},
            ]},
        ],
    }])
    prompt = ts.to_prompt()
    assert '"payload": an object with keys:' in prompt
    assert '"inner": an object with keys:' in prompt
    assert '"z": boolean (required, never null)' in prompt

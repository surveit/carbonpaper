"""Tests for the schema capabilities in app/models/schema.py: Column.enum, the
recursive json/list[json] shape (Column.fields / Column.value_type),
TableSchema.subtract (strict=True and strict=False) / is_subset_of, and
TableSchema.to_prompt."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app import models as m


# ── Column.enum ──────────────────────────────────────────────────────────────
def test_enum_valid_on_str_column():
    c = m.Column.model_validate({"name": "status", "type": "str", "enum": ["a", "b"], "nullable": True})
    assert c.enum == ["a", "b"]


def test_enum_empty_list_rejected():
    with pytest.raises(ValidationError):
        m.Column.model_validate({"name": "status", "type": "str", "enum": [], "nullable": True})


def test_enum_on_non_str_type_rejected():
    with pytest.raises(ValidationError):
        m.Column.model_validate({"name": "count", "type": "int", "enum": ["a", "b"], "nullable": True})


# ── dict type rejected ───────────────────────────────────────────────────────
def test_dict_type_rejected():
    with pytest.raises(ValidationError):
        m.Column.model_validate({"name": "payload", "type": "dict", "nullable": True})


# ── Column recursive json shape ──────────────────────────────────────────────
def test_json_with_fields_object():
    c = m.Column.model_validate({
        "name": "payload", "type": "json",
        "fields": [
            {"name": "x", "type": "str", "nullable": False},
            {"name": "y", "type": "int", "nullable": True},
        ],
    "nullable": True})
    assert c.value_type is None
    assert [f.name for f in c.fields] == ["x", "y"]


def test_json_with_nested_fields():
    c = m.Column.model_validate({
        "name": "payload", "type": "json",
        "fields": [
            {"name": "inner", "type": "json", "fields": [
                {"name": "z", "type": "str", "nullable": False},
            ], "nullable": True},
        ],
    "nullable": True})
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
    "nullable": True})
    assert c.type == "list[json]"
    assert [f.name for f in c.fields] == ["topic", "score"]


def test_json_with_value_type_open_map():
    c = m.Column.model_validate({
        "name": "url_map", "type": "json", "value_type": "str",
    "nullable": True})
    assert c.value_type == "str"
    assert c.fields is None


def test_list_json_with_value_type_open_map():
    c = m.Column.model_validate({
        "name": "url_maps", "type": "list[json]", "value_type": "str",
    "nullable": True})
    assert c.value_type == "str"


def test_json_value_type_must_be_scalar():
    with pytest.raises(ValidationError):
        m.Column.model_validate({"name": "payload", "type": "json", "value_type": "json", "nullable": True})


def test_json_neither_fields_nor_value_type_rejected():
    with pytest.raises(ValidationError):
        m.Column.model_validate({"name": "payload", "type": "json", "nullable": True})


def test_json_both_fields_and_value_type_rejected():
    with pytest.raises(ValidationError):
        m.Column.model_validate({
            "name": "payload", "type": "json",
            "fields": [{"name": "x", "type": "str", "nullable": True}],
            "value_type": "str",
        "nullable": True})


def test_fields_forbidden_on_non_json_type():
    with pytest.raises(ValidationError):
        m.Column.model_validate({
            "name": "count", "type": "int",
            "fields": [{"name": "x", "type": "str", "nullable": True}],
        "nullable": True})


def test_value_type_forbidden_on_non_json_type():
    with pytest.raises(ValidationError):
        m.Column.model_validate({"name": "count", "type": "int", "value_type": "str", "nullable": True})


# ── TableSchema.subtract ─────────────────────────────────────────────────────
def _ts(**kwargs):
    return m.TableSchema.model_validate(kwargs)


def test_subtract_difference():
    a = _ts(columns=[{"name": "id", "type": "str", "nullable": True}, {"name": "name", "type": "str", "nullable": True}])
    b = _ts(columns=[
        {"name": "id", "type": "str", "nullable": True},
        {"name": "name", "type": "str", "nullable": True},
        {"name": "score", "type": "int", "nullable": True},
    ])
    diff = b.subtract(a)
    assert [c.name for c in diff.columns] == ["score"]


def test_subtract_result_has_no_metadata():
    a = _ts(columns=[{"name": "id", "type": "str", "nullable": True}])
    b = _ts(
        columns=[{"name": "id", "type": "str", "nullable": True}, {"name": "score", "type": "int", "nullable": True}], estimated_rows=10, notes="some notes",
    )
    diff = b.subtract(a)
    assert diff.estimated_rows is None
    assert diff.notes is None


def test_subtract_identical_spec_shared_column_is_fine():
    a = _ts(columns=[{"name": "id", "type": "str", "nullable": False}])
    b = _ts(columns=[
        {"name": "id", "type": "str", "nullable": False},
        {"name": "score", "type": "int", "nullable": True},
    ])
    diff = b.subtract(a)
    assert [c.name for c in diff.columns] == ["score"]


def test_subtract_description_only_difference_does_not_throw():
    a = _ts(columns=[{"name": "id", "type": "str", "description": "from producer", "nullable": True}])
    b = _ts(columns=[
        {"name": "id", "type": "str", "description": "from consumer", "nullable": True},
        {"name": "score", "type": "int", "nullable": True},
    ])
    diff = b.subtract(a)
    assert [c.name for c in diff.columns] == ["score"]


def test_subtract_source_only_difference_does_not_throw():
    a = _ts(columns=[{"name": "id", "type": "str", "source": "stage_a", "nullable": True}])
    b = _ts(columns=[
        {"name": "id", "type": "str", "source": "stage_b", "nullable": True},
        {"name": "score", "type": "int", "nullable": True},
    ])
    diff = b.subtract(a)
    assert [c.name for c in diff.columns] == ["score"]


def test_subtract_type_mismatch_throws():
    a = _ts(columns=[{"name": "id", "type": "str", "nullable": True}])
    b = _ts(columns=[{"name": "id", "type": "int", "nullable": True}])
    with pytest.raises(ValueError, match="id"):
        b.subtract(a)


def test_subtract_nullable_mismatch_throws():
    a = _ts(columns=[{"name": "id", "type": "str", "nullable": True}])
    b = _ts(columns=[{"name": "id", "type": "str", "nullable": False}])
    with pytest.raises(ValueError, match="id"):
        b.subtract(a)


def test_subtract_enum_mismatch_throws():
    a = _ts(columns=[{"name": "status", "type": "str", "enum": ["a", "b"], "nullable": True}])
    b = _ts(columns=[{"name": "status", "type": "str", "enum": ["a", "c"], "nullable": True}])
    with pytest.raises(ValueError, match="status"):
        b.subtract(a)


def test_subtract_range_mismatch_throws():
    a = _ts(columns=[{"name": "score", "type": "int", "range": [0, 10], "nullable": True}])
    b = _ts(columns=[{"name": "score", "type": "int", "range": [0, 100], "nullable": True}])
    with pytest.raises(ValueError, match="score"):
        b.subtract(a)


def test_subtract_fields_identical_passes():
    fields = [{"name": "x", "type": "str", "nullable": False}]
    a = _ts(columns=[{"name": "payload", "type": "json", "fields": fields, "nullable": True}])
    b = _ts(columns=[
        {"name": "payload", "type": "json", "fields": fields, "nullable": True},
        {"name": "score", "type": "int", "nullable": True},
    ])
    diff = b.subtract(a)
    assert [c.name for c in diff.columns] == ["score"]


def test_subtract_fields_differing_throws():
    a = _ts(columns=[{
        "name": "payload", "type": "json",
        "fields": [{"name": "x", "type": "str", "nullable": False}],
    "nullable": True}])
    b = _ts(columns=[{
        "name": "payload", "type": "json",
        "fields": [{"name": "x", "type": "int", "nullable": False}],
    "nullable": True}])
    with pytest.raises(ValueError, match="payload"):
        b.subtract(a)


def test_subtract_value_type_mismatch_throws():
    a = _ts(columns=[{"name": "url_map", "type": "json", "value_type": "str", "nullable": True}])
    b = _ts(columns=[{"name": "url_map", "type": "json", "value_type": "int", "nullable": True}])
    with pytest.raises(ValueError, match="url_map"):
        b.subtract(a)


def test_subtract_nested_field_prose_difference_does_not_throw():
    a = _ts(columns=[{
        "name": "payload", "type": "json",
        "fields": [{"name": "x", "type": "str", "nullable": False, "description": "from producer"}],
    "nullable": True}])
    b = _ts(columns=[
        {"name": "payload", "type": "json",
         "fields": [{"name": "x", "type": "str", "nullable": False, "description": "from consumer"}], "nullable": True},
        {"name": "score", "type": "int", "nullable": True},
    ])
    diff = b.subtract(a)
    assert [c.name for c in diff.columns] == ["score"]


def test_subtract_nested_field_spec_difference_throws():
    a = _ts(columns=[{
        "name": "payload", "type": "json",
        "fields": [{"name": "x", "type": "str", "nullable": True}],
    "nullable": True}])
    b = _ts(columns=[{
        "name": "payload", "type": "json",
        "fields": [{"name": "x", "type": "str", "nullable": False}],
    "nullable": True}])
    with pytest.raises(ValueError, match="payload"):
        b.subtract(a)


def test_spec_column_fields_read_off_the_model():
    """So a newly added Column field is compared by subtract instead of being silently ignored."""
    from app.models import schema as sch
    prose = {"name", "description", "source"}
    assert set(sch._SPEC_COLUMN_FIELDS) == set(m.Column.model_fields) - prose


# ── TableSchema.column_for_name ───────────────────────────────────────────────
def test_column_for_name_finds_by_name_or_returns_none():
    a = _ts(columns=[{"name": "id", "type": "str", "nullable": True}, {"name": "score", "type": "int", "nullable": True}])
    col = a.column_for_name("score")
    assert col is not None
    assert col.name == "score"
    assert col.type == "int"
    assert a.column_for_name("gone") is None


# ── TableSchema.is_subset_of ─────────────────────────────────────────────────
def test_is_subset_of_true_when_present_and_identical():
    a = _ts(columns=[{"name": "id", "type": "str", "nullable": True}])
    b = _ts(columns=[{"name": "id", "type": "str", "nullable": True}, {"name": "score", "type": "int", "nullable": True}])
    assert a.is_subset_of(b) is True


def test_is_subset_of_false_when_column_absent():
    a = _ts(columns=[{"name": "id", "type": "str", "nullable": True}, {"name": "gone", "type": "str", "nullable": True}])
    b = _ts(columns=[{"name": "id", "type": "str", "nullable": True}])
    assert a.is_subset_of(b) is False


def test_is_subset_of_false_when_spec_differs():
    a = _ts(columns=[{"name": "id", "type": "str", "nullable": True}])
    b = _ts(columns=[{"name": "id", "type": "int", "nullable": True}])
    assert a.is_subset_of(b) is False


def test_is_subset_of_ignores_prose():
    a = _ts(columns=[{"name": "id", "type": "str", "description": "producer", "nullable": True}])
    b = _ts(columns=[{"name": "id", "type": "str", "description": "consumer", "nullable": True}])
    assert a.is_subset_of(b) is True


# ── TableSchema.subtract(strict=False) ───────────────────────────────────────
def test_subtract_strict_false_empty_when_subset():
    a = _ts(columns=[{"name": "id", "type": "str", "nullable": True}])
    b = _ts(columns=[{"name": "id", "type": "str", "nullable": True}, {"name": "score", "type": "int", "nullable": True}])
    assert a.subtract(b, strict=False).columns == []
    assert a.is_subset_of(b) is True


def test_subtract_strict_false_lists_absent_column():
    a = _ts(columns=[{"name": "id", "type": "str", "nullable": True}, {"name": "gone", "type": "str", "nullable": True}])
    b = _ts(columns=[{"name": "id", "type": "str", "nullable": True}])
    missing = a.subtract(b, strict=False).columns
    assert [c.name for c in missing] == ["gone"]
    assert a.is_subset_of(b) is False


# ── TableSchema.find_unsatisfied_columns ─────────────────────────────────────
# The requirement→producer direction: `self` names the columns a consumer needs,
# the argument names what a producer emits. Returns one reason per column the
# producer fails to satisfy ([] ⇒ producer supplies every required column).
def test_find_unsatisfied_columns_empty_when_producer_covers_every_column():
    required = _ts(columns=[{"name": "id", "type": "str", "nullable": True}])
    producer = _ts(columns=[{"name": "id", "type": "str", "nullable": True}, {"name": "score", "type": "int", "nullable": True}])
    assert required.find_unsatisfied_columns(producer) == []


def test_find_unsatisfied_columns_flags_column_absent_from_producer():
    required = _ts(columns=[{"name": "id", "type": "str", "nullable": True}, {"name": "quote", "type": "str", "nullable": True}])
    producer = _ts(columns=[{"name": "id", "type": "str", "nullable": True}])
    reasons = required.find_unsatisfied_columns(producer)
    assert len(reasons) == 1
    assert "quote" in reasons[0] and "absent" in reasons[0]


def test_find_unsatisfied_columns_flags_type_difference():
    required = _ts(columns=[{"name": "score", "type": "str", "nullable": True}])
    producer = _ts(columns=[{"name": "score", "type": "int", "nullable": True}])
    reasons = required.find_unsatisfied_columns(producer)
    assert len(reasons) == 1
    assert "score" in reasons[0] and "type" in reasons[0]


def test_find_unsatisfied_columns_flags_required_non_null_fed_by_nullable_producer():
    required = _ts(columns=[{"name": "score", "type": "int", "nullable": False}])
    producer = _ts(columns=[{"name": "score", "type": "int", "nullable": True}])
    reasons = required.find_unsatisfied_columns(producer)
    assert len(reasons) == 1
    assert "nullable" in reasons[0]


def test_find_unsatisfied_columns_allows_nullable_requirement_fed_by_non_null_producer():
    required = _ts(columns=[{"name": "score", "type": "int", "nullable": True}])
    producer = _ts(columns=[{"name": "score", "type": "int", "nullable": False}])
    assert required.find_unsatisfied_columns(producer) == []


def test_find_unsatisfied_columns_ignores_prose_difference():
    required = _ts(columns=[{"name": "id", "type": "str", "description": "consumer note", "nullable": True}])
    producer = _ts(columns=[{"name": "id", "type": "str", "description": "producer note", "nullable": True}])
    assert required.find_unsatisfied_columns(producer) == []


def test_find_unsatisfied_columns_reports_every_offending_column():
    required = _ts(columns=[
        {"name": "id", "type": "str", "nullable": True},
        {"name": "quote", "type": "str", "nullable": True},
        {"name": "score", "type": "str", "nullable": True},
    ])
    producer = _ts(columns=[{"name": "id", "type": "str", "nullable": True}, {"name": "score", "type": "int", "nullable": True}])
    reasons = required.find_unsatisfied_columns(producer)
    assert len(reasons) == 2  # quote absent + score type-differs — all at once, not just the first


def test_is_subset_of_uses_exact_nullability():
    nullable = _ts(columns=[{"name": "score", "type": "int", "nullable": True}])
    non_null = _ts(columns=[{"name": "score", "type": "int", "nullable": False}])
    assert nullable.is_subset_of(non_null) is False
    assert nullable.find_unsatisfied_columns(non_null) == []


def test_subtract_strict_false_lists_column_with_differing_spec():
    a = _ts(columns=[{"name": "id", "type": "str", "nullable": True}])
    b = _ts(columns=[{"name": "id", "type": "int", "nullable": True}])
    missing = a.subtract(b, strict=False).columns
    assert [c.name for c in missing] == ["id"]
    assert a.is_subset_of(b) is False


def test_subtract_strict_false_ignores_prose_differences():
    a = _ts(columns=[{"name": "id", "type": "str", "description": "producer", "nullable": True}])
    b = _ts(columns=[{"name": "id", "type": "str", "description": "consumer", "nullable": True}])
    assert a.subtract(b, strict=False).columns == []


def test_subtract_strict_false_does_not_throw_on_spec_delta():
    a = _ts(columns=[{"name": "id", "type": "str", "nullable": True}])
    b = _ts(columns=[{"name": "id", "type": "int", "nullable": True}])
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


def test_to_prompt_says_nothing_about_a_key_that_is_not_declared():
    ts = _ts(columns=[{"name": "id", "type": "str", "nullable": True}])
    assert "Primary key" not in ts.to_prompt()


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
    with pytest.raises(ValidationError, match="enum"):
        m.Column(name="source_class", type="str",
                 range=["org_websites", "corporate_media", "CDP"], nullable=True)


def test_numeric_range_on_str_column_rejected():
    with pytest.raises(ValidationError, match="range"):
        m.Column(name="code", type="str", range=[0, 10], nullable=True)


def test_range_must_be_exactly_two_numbers():
    with pytest.raises(ValidationError, match="range"):
        m.Column(name="score", type="int", range=[0], nullable=True)


def test_range_numeric_pair_on_numeric_column_valid():
    col = m.Column(name="score", type="int", range=[0, 100], nullable=True)
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

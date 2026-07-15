"""TableSchema.to_pydantic_model: the compiled Pydantic model enforces the
schema recursively — presence, types, nullability, enum vocabulary, numeric
range, nested json/list[json] fields — and rejects unknown keys."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.models import TableSchema


def _model(cols):
    return TableSchema.model_validate({"columns": cols}).to_pydantic_model("reply")


def test_valid_row_roundtrips():
    model = _model([
        {"name": "score", "type": "int", "nullable": False},
        {"name": "note", "type": "str"},
    ])
    got = model.model_validate({"score": 3, "note": None})
    assert got.model_dump() == {"score": 3, "note": None}


def test_missing_key_rejected():
    # nullable ≠ omittable: every declared column must appear in the reply
    model = _model([{"name": "note", "type": "str"}])
    with pytest.raises(ValidationError):
        model.model_validate({})


def test_null_in_non_nullable_rejected():
    model = _model([{"name": "score", "type": "int", "nullable": False}])
    with pytest.raises(ValidationError):
        model.model_validate({"score": None})


def test_unknown_key_rejected():
    model = _model([{"name": "score", "type": "int", "nullable": False}])
    with pytest.raises(ValidationError):
        model.model_validate({"score": 1, "bonus": 2})


def test_enum_vocabulary_enforced():
    model = _model([{"name": "stance", "type": "str", "nullable": False,
                     "enum": ["supports", "opposes"]}])
    assert model.model_validate({"stance": "supports"}).model_dump() == {"stance": "supports"}
    with pytest.raises(ValidationError):
        model.model_validate({"stance": "meh"})


def test_numeric_range_enforced():
    model = _model([{"name": "score", "type": "int", "nullable": False, "range": [0, 5]}])
    assert model.model_validate({"score": 5}).model_dump() == {"score": 5}
    with pytest.raises(ValidationError):
        model.model_validate({"score": 6})


def test_inf_range_bound_means_unbounded():
    model = _model([{"name": "usd", "type": "float", "nullable": False, "range": [0, "+inf"]}])
    assert model.model_validate({"usd": 1e12}).model_dump() == {"usd": 1e12}
    with pytest.raises(ValidationError):
        model.model_validate({"usd": -1.0})


def test_list_of_scalars():
    model = _model([{"name": "tags", "type": "list[str]", "nullable": False}])
    assert model.model_validate({"tags": ["a", "b"]}).model_dump() == {"tags": ["a", "b"]}
    with pytest.raises(ValidationError):
        model.model_validate({"tags": "not-a-list"})


def test_list_json_elements_validated_recursively():
    model = _model([
        {"name": "claims", "type": "list[json]", "nullable": False, "fields": [
            {"name": "text", "type": "str", "nullable": False},
            {"name": "stance", "type": "str", "nullable": False,
             "enum": ["supports", "opposes"]},
        ]},
    ])
    ok = model.model_validate(
        {"claims": [{"text": "t", "stance": "supports"}]})
    assert ok.model_dump() == {"claims": [{"text": "t", "stance": "supports"}]}
    with pytest.raises(ValidationError):  # bad element enum, one level down
        model.model_validate({"claims": [{"text": "t", "stance": "meh"}]})
    with pytest.raises(ValidationError):  # missing element key, one level down
        model.model_validate({"claims": [{"text": "t"}]})


def test_json_value_type_open_map():
    model = _model([{"name": "meta", "type": "json", "nullable": False,
                     "value_type": "int"}])
    assert model.model_validate({"meta": {"a": 1}}).model_dump() == {"meta": {"a": 1}}
    with pytest.raises(ValidationError):
        model.model_validate({"meta": {"a": "x"}})


def test_description_carried_into_json_schema():
    model = _model([{"name": "score", "type": "int", "nullable": False,
                     "description": "0 worst, 5 best"}])
    props = model.model_json_schema()["properties"]
    assert props["score"]["description"] == "0 worst, 5 best"


def test_scalar_vocabulary_matches_schema_layer():
    # row_model's python-type map must cover exactly the schema layer's scalars
    from app.core.models.row_model import _SCALAR_PY_TYPES
    from app.core.models.schema import SCALAR_COLUMN_TYPES
    assert set(_SCALAR_PY_TYPES) == SCALAR_COLUMN_TYPES

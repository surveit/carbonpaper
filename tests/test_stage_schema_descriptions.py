"""The validator rules a compiling agent keeps tripping over must be documented in the
FIELD DESCRIPTIONS, because the submit_answer tool's input schema is
`Workflow.model_json_schema()` (app/core/agent/agent.py) — so a `Field(description=...)` is the
only channel that reaches the model at the point it fills the field. These assert each rule
that currently lives only in a validator is also stated in the relevant field's description.
"""
from __future__ import annotations

from pydantic import BaseModel

from app.models.schema import Column
from app.models.stage import Connector, PythonFunction, Stage


def _desc(model: type[BaseModel], field: str) -> str:
    s = model.model_json_schema()
    # A self-referential model (e.g. Column.fields: list[Column]) returns a $ref wrapper.
    props = s.get("properties") or s["$defs"][model.__name__]["properties"]
    return (props[field].get("description") or "").lower()


def test_connector_params_documents_optional_absolute_path_and_bans_invention():
    # validator: path optional; absolute when present (stage.py). The description is
    # the only channel that reaches the compiling agent (submit_answer tool schema).
    d = _desc(Connector, "params")
    assert "absolute" in d
    assert "omit" in d
    assert "never invent" in d
    assert "placeholder" not in d


def test_output_schema_documents_llm_transform_additive_rule():
    # validator: `llm_transform not strictly 1:1 ... additive ... primary_key` (stage.py) — #2
    d = _desc(Stage, "output_schema")
    assert "additive" in d
    assert "primary_key" in d


def test_function_code_documents_the_three_signatures():
    # the runtime calls fn(row) / fn(*frames) / fn(*frames, output_dir=...) — #3, #6
    d = _desc(PythonFunction, "code")
    assert "def transform(row" in d.replace("`", "")   # python_row_function
    assert "frame" in d                                 # python_frame_function
    assert "output_dir" in d                            # publish


def test_column_type_documents_json_and_list_forms():
    # validator: json/list[json] columns need fields or value_type (schema.py) — #5
    d = _desc(Column, "type")
    assert "list[" in d
    assert "json" in d

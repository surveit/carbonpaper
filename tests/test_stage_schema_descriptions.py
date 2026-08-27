"""The validator rules a compiling agent keeps tripping over must be documented in the
FIELD DESCRIPTIONS, because the submit_answer tool's input schema is
`Workflow.model_json_schema()` (app/core/agent/agent.py) — so a `Field(description=...)` is the
only channel that reaches the model at the point it fills the field. These assert each rule
that currently lives only in a validator is also stated in the relevant field's description.
"""
from __future__ import annotations

from pydantic import BaseModel

from app.models.stages.stage_types import STAGE_TYPES
from app.models.schema import Column
from app.models.stage import PythonFunction, AbstractStage
from app.models.stages.input_data import Connector


def _desc(model: type[BaseModel], field: str) -> str:
    s = model.model_json_schema()
    # A self-referential model (e.g. Column.fields: list[Column]) returns a $ref wrapper.
    props = s.get("properties") or s["$defs"][model.__name__]["properties"]
    return (props[field].get("description") or "").lower()


def test_connector_params_documents_optional_absolute_path_and_bans_invention():
    d = _desc(Connector, "params")
    assert "absolute" in d
    assert "omit" in d
    assert "never invent" in d
    assert "placeholder" not in d


def test_llm_transform_notes_document_the_additive_rule():
    notes = STAGE_TYPES["llm_transform"].notes.lower()
    assert "additive" in notes


def test_inputs_document_what_an_upstream_supplies():
    d = _desc(AbstractStage, "inputs")
    assert "upstream stage id" in d and "upstream stage's own output schema" in d


def test_function_code_documents_the_three_signatures():
    # the runtime calls fn(row) / fn(*frames) / fn(*frames, output_dir=...) — #3, #6
    d = _desc(PythonFunction, "code")
    assert "def transform(row" in d.replace("`", "")   # python_row_function
    assert "frame" in d                                 # python_frame_function
    assert "output_dir" in d                            # report


def test_column_type_documents_json_and_list_forms():
    # validator: json/list[json] columns need fields or value_type (schema.py) — #5
    d = _desc(Column, "type")
    assert "list[" in d
    assert "json" in d

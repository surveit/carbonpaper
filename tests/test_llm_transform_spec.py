"""The one thing llm_transform adds over a plain LLM call: it compiles the
derived reply spec — output_schema − input_schema — to the Pydantic model the
agent backend enforces. The call mechanism itself is unchanged (llm.call_llm
per row, driven by the runtime's row driver)."""
from __future__ import annotations

import asyncio

import pandas as pd
import pytest
from pydantic import ValidationError

from app.models import Stage
from app.models.stage import StageType
from app.runtime.stages import HANDLERS
from app.runtime.stages import llm_transform as lt
from conftest import make_run_context


def _stage():
    return Stage.model_validate({
        "id": "score", "name": "score", "type": "llm_transform",
        "inputs": [{"id": "load", "schema": {
            "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}],
            "primary_key": ["id"]}}],
        "output_schema": {
            "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"},
                        {"name": "score", "type": "int", "nullable": False}],
            "primary_key": ["id"]},
        "llm": {"prompt_template": "Rate: {text}"},
    })


def _run(stage, frames, ctx=None):
    return HANDLERS[StageType.llm_transform].execute(
        stage, frames, ctx if ctx is not None else make_run_context())


def test_reply_model_is_the_subtracted_spec(monkeypatch):
    captured: dict[str, object] = {}

    def fake_call(stage_id, llm_config, row, *, reply_model, **kw):
        captured["fields"] = set(reply_model.model_fields)
        captured["template"] = llm_config.prompt_data_template
        return {"score": 5}

    monkeypatch.setattr(lt, "call_llm", fake_call)
    _run(_stage(), {"load": pd.DataFrame({"id": ["r1"], "text": ["hi"]})})

    assert captured["fields"] == {"score"}            # added column asked for…
    # …passthrough columns are not: they ride through from the input row.
    assert captured["template"] == "Rate: {text}"     # template reaches the backend unaltered


def test_reply_model_enforces_the_spec():
    # the model built for the stage rejects a wrong-shaped reply outright
    stage = _stage()
    spec = stage.output_schema.subtract(stage.inputs[0].table_schema)
    model = spec.to_pydantic_model("score_reply")
    with pytest.raises(ValidationError):
        model.model_validate({"score": "not-a-number-at-all"})


def test_output_rows_carry_reply_columns(monkeypatch):
    monkeypatch.setattr(lt, "call_llm", lambda *a, **k: {"score": 7})
    out = _run(_stage(), {"load": pd.DataFrame({"id": ["r1"], "text": ["hi"]})})
    assert out.loc[0, "score"] == 7
    assert out.loc[0, "id"] == "r1"


def test_backend_error_surfaces_as_row_error_not_raised(monkeypatch):
    def boom(stage_id, llm_config, row, **kw):
        raise RuntimeError("backend down")

    monkeypatch.setattr(lt, "call_llm", boom)
    ctx = make_run_context()
    out = _run(_stage(), {"load": pd.DataFrame({"id": ["r1"], "text": ["hi"]})}, ctx)
    assert len(out) == 1                                    # not raised; stage completes
    assert ctx.row_errors["score"] == [{"row": 0, "message": "backend down"}]


def test_timeout_with_empty_message_is_captured_and_labeled(monkeypatch):
    # asyncio.TimeoutError() (the real timeout path in app/runtime/llm.py)
    # stringifies to "" — a message-less failure must still be captured (not
    # mistaken for a successful row) and labeled with the exception's type name.
    def boom(stage_id, llm_config, row, **kw):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(lt, "call_llm", boom)
    ctx = make_run_context()
    out = _run(_stage(), {"load": pd.DataFrame({"id": ["r1"], "text": ["hi"]})}, ctx)
    assert len(out) == 1                                    # not raised; stage completes
    assert ctx.row_errors["score"] == [{"row": 0, "message": "TimeoutError"}]

"""The one thing llm_transform adds over a plain LLM call: it appends the
derived reply spec (output_schema − input_schema, rendered by
TableSchema.to_prompt) to the prompt. The call mechanism itself is unchanged
(llm.call_llm per row, driven by the runtime's row driver)."""
from __future__ import annotations

import pandas as pd

from app.core.models import Stage
from app.core.models.stage import StageType
from app.runtime.stages import HANDLERS
from app.runtime.stages import llm_transform as lt


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
        stage, frames, ctx if ctx is not None else {})


def test_reply_spec_appended_to_prompt(monkeypatch):
    captured: dict[str, str] = {}

    def fake_call(stage_id, llm_config, row, **kw):
        captured["template"] = llm_config.prompt_template
        return {"score": 5}

    monkeypatch.setattr(lt, "call_llm", fake_call)
    _run(_stage(), {"load": pd.DataFrame({"id": ["r1"], "text": ["hi"]})})

    template = captured["template"]
    assert "Rate: {text}" in template                      # original template preserved
    assert "Return ONE JSON object only" in template       # reply-spec header
    reply_section = template.split("Return ONE JSON object only")[1]
    assert '"score"' in reply_section                      # the derived new column is asked for
    assert '"text"' not in reply_section                   # passthrough columns are NOT asked for


def test_output_rows_carry_reply_columns(monkeypatch):
    monkeypatch.setattr(lt, "call_llm", lambda *a, **k: {"score": 7})
    out = _run(_stage(), {"load": pd.DataFrame({"id": ["r1"], "text": ["hi"]})})
    assert out.loc[0, "score"] == 7
    assert out.loc[0, "id"] == "r1"


def test_list_reply_is_a_value_not_rows(monkeypatch):
    # A JSON-list reply is data, not rows: exactly one output row per input row,
    # the reply kept whole in _raw (then dropped-and-recorded by projection,
    # since _raw is undeclared). The declared reply column is absent — output
    # schema validation surfaces that on the run; nothing is invented.
    monkeypatch.setattr(lt, "call_llm", lambda *a, **k: [{"score": 1}, {"score": 2}])
    ctx: dict = {}
    out = _run(_stage(), {"load": pd.DataFrame({"id": ["r1"], "text": ["hi"]})}, ctx)
    assert len(out) == 1                                   # 1:1 — never fans out
    assert list(out.columns) == ["id", "text"]             # "score" absent, not invented
    assert "_raw" in ctx["dropped_columns"]["score"]       # reply preserved + recorded
                                                           # ("score" = the stage id)


def test_backend_error_is_recorded_per_row_not_raised(monkeypatch):
    def boom(stage_id, llm_config, row, **kw):
        raise RuntimeError("backend down")

    monkeypatch.setattr(lt, "call_llm", boom)
    ctx: dict = {}
    out = _run(_stage(), {"load": pd.DataFrame({"id": ["r1"], "text": ["hi"]})}, ctx)
    assert len(out) == 1                                   # still 1:1 — row survives
    assert "_error" in ctx["dropped_columns"]["score"]     # recorded, not silent

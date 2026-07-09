"""The one thing llm_transform adds over a plain LLM call: it appends the
derived reply spec (output_schema − input_schema, rendered by
TableSchema.to_prompt) to the prompt. The call mechanism itself is unchanged
(master's llm.call_llm_batch)."""
from __future__ import annotations

import pandas as pd

from app.models import Stage
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


def test_reply_spec_appended_to_prompt(monkeypatch):
    captured: dict[str, str] = {}

    def fake_batch(stage_id, llm_config, rows, **kw):
        captured["template"] = llm_config.prompt_template
        return [{"score": 5} for _ in rows]

    monkeypatch.setattr(lt, "call_llm_batch", fake_batch)
    lt.handle_llm_transform(_stage(), {"load": pd.DataFrame({"id": ["r1"], "text": ["hi"]})}, {})

    template = captured["template"]
    assert "Rate: {text}" in template                      # original template preserved
    assert "Return ONE JSON object only" in template       # reply-spec header
    reply_section = template.split("Return ONE JSON object only")[1]
    assert '"score"' in reply_section                      # the derived new column is asked for
    assert '"text"' not in reply_section                   # passthrough columns are NOT asked for


def test_output_rows_carry_reply_columns(monkeypatch):
    monkeypatch.setattr(lt, "call_llm_batch", lambda *a, **k: [{"score": 7}])
    out = lt.handle_llm_transform(
        _stage(), {"load": pd.DataFrame({"id": ["r1"], "text": ["hi"]})}, {}
    )
    assert out.loc[0, "score"] == 7
    assert out.loc[0, "id"] == "r1"

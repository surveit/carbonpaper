"""Pins the fix for issue #50: llm_transform / human_review_queue project their
output onto EXACTLY the columns declared in output_schema — never a runtime
keep-list of methodology-specific column names. A column that must survive the
stage (including a stable id carried through from the input row) earns its place
by being declared in output_schema — mark a carried-through column
source: passthrough. Anything the schema doesn't declare is dropped and recorded
on ctx["dropped_columns"], not silently discarded."""
from __future__ import annotations

import pandas as pd

from app.models import Stage
from app.runtime.stages import handle_human_review_queue, handle_llm_transform


def _llm_stage(output_schema=None):
    llm = {"prompt_template": "extract from {doc_id}"}
    kw = {
        "id": "evidence_extraction", "name": "Extract evidence",
        "type": "llm_transform", "inputs": [{"id": "load"}],
        "llm": llm,
    }
    if output_schema is not None:
        kw["output_schema"] = output_schema
    return Stage.model_validate(kw)


def _src_docs():
    # entity_id NOT prefixed 'M:' selects the LobbyMap keyword table in the
    # offline mock; the sentence matches the carbon-tax pattern.
    return pd.DataFrame([
        {"doc_id": "d1", "entity_id": "C:acme", "body": "We strongly support a carbon tax."},
    ])


def test_llm_transform_keeps_only_declared_columns(monkeypatch):
    monkeypatch.setenv("CW_LLM_FORCE_MOCK", "1")
    stage = _llm_stage(output_schema={"columns": [{"name": "evidence_id", "type": "str"},
                                                   {"name": "quote", "type": "str"}]})
    ctx: dict = {}
    out = handle_llm_transform(stage, {"load": _src_docs()}, ctx)

    # Pure projection: exactly the declared columns, nothing else — no
    # 'doc_id'/'entity_id'/'query_id' resurrected by a hardcoded keep-list.
    assert list(out.columns) == ["evidence_id", "quote"]

    # The columns the LLM/mock produced but that aren't declared were dropped,
    # and the drop is recorded rather than silent.
    dropped = ctx["dropped_columns"]["evidence_extraction"]
    for col in ("doc_id", "entity_id", "body", "query_id", "stance_summary"):
        assert col in dropped


def test_llm_transform_carried_id_columns_survive_by_being_declared(monkeypatch):
    # A stable id the stage needs downstream survives because it's *declared* in
    # output_schema (source: passthrough documents that it rides through from the
    # input row) — not because the runtime keeps a magic list of column names.
    monkeypatch.setenv("CW_LLM_FORCE_MOCK", "1")
    stage = _llm_stage(output_schema={"columns": [
        {"name": "evidence_id", "type": "str"},
        {"name": "quote", "type": "str"},
        {"name": "doc_id", "type": "str", "source": "passthrough"},
        {"name": "entity_id", "type": "str", "source": "passthrough"},
    ]})
    ctx: dict = {}
    out = handle_llm_transform(stage, {"load": _src_docs()}, ctx)

    # Output order is the schema's declared order — the author controls it there.
    assert list(out.columns) == ["evidence_id", "quote", "doc_id", "entity_id"]
    dropped = ctx["dropped_columns"]["evidence_extraction"]
    assert "doc_id" not in dropped and "entity_id" not in dropped
    assert "query_id" in dropped  # still dropped: not declared


def test_llm_transform_no_output_schema_keeps_everything(monkeypatch):
    """No declared schema = no contract to project onto — unchanged behavior,
    and nothing recorded as dropped (nothing WAS dropped)."""
    monkeypatch.setenv("CW_LLM_FORCE_MOCK", "1")
    stage = _llm_stage(output_schema=None)
    ctx: dict = {}
    out = handle_llm_transform(stage, {"load": _src_docs()}, ctx)
    assert "query_id" in out.columns
    assert "evidence_extraction" not in ctx.get("dropped_columns", {})


def _queue_stage(output_schema=None, flt=None):
    queue = {"hash_columns": ["entity_id"]}
    if flt is not None:
        queue["filter"] = flt
    kw = {
        "id": "review", "name": "Human review", "type": "human_review_queue",
        "inputs": [{"id": "scored"}], "queue": queue,
    }
    if output_schema is not None:
        kw["output_schema"] = output_schema
    return Stage.model_validate(kw)


def _src_scored():
    # filter="entity_id == 'nope'" matches nothing, so every row is a
    # pass-through row — avoids HaltForReview so the test can assert on the
    # projected output directly.
    return pd.DataFrame([
        {"entity_id": "C:acme", "evidence_id": "d1#0", "quote": "we love climate policy",
         "score": 1, "benchmark_id": "B1", "query_id": "Q5"},
    ])


def test_human_review_queue_keeps_only_declared_columns(tmp_path):
    stage = _queue_stage(
        output_schema={"columns": [{"name": "evidence_id", "type": "str"},
                                    {"name": "final_score", "type": "int"}]},
        flt="entity_id == 'nope'",
    )
    ctx = {"project_dir": tmp_path, "run_dir": tmp_path}
    out = handle_human_review_queue(stage, {"scored": _src_scored()}, ctx)

    assert list(out.columns) == ["evidence_id", "final_score"]
    dropped = ctx["dropped_columns"]["review"]
    for col in ("entity_id", "quote", "benchmark_id", "query_id"):
        assert col in dropped


def test_human_review_queue_carried_columns_survive_by_being_declared(tmp_path):
    # `quote` survives because it's declared in output_schema (source:
    # passthrough), not because the runtime keeps a magic list of column names.
    stage = _queue_stage(
        output_schema={"columns": [{"name": "evidence_id", "type": "str"},
                                    {"name": "final_score", "type": "int"},
                                    {"name": "quote", "type": "str", "source": "passthrough"}]},
        flt="entity_id == 'nope'",
    )
    ctx = {"project_dir": tmp_path, "run_dir": tmp_path}
    out = handle_human_review_queue(stage, {"scored": _src_scored()}, ctx)

    assert list(out.columns) == ["evidence_id", "final_score", "quote"]
    dropped = ctx["dropped_columns"]["review"]
    assert "quote" not in dropped
    assert "benchmark_id" in dropped  # still dropped: not declared

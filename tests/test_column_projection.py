"""Pins issue #50: output_schema alone decides which columns survive a stage.
"""
from __future__ import annotations

import pandas as pd

from app.models import parse_stage
from app.models.stage import StageType
from app.runtime.context import RunContext, RunIdentity
from app.runtime.stages import HANDLERS
from app.runtime.stages import llm_transform as lt
from app.core.stage_cache import StageCacheEntry
from conftest import contribution_of, make_run_context


def _llm_stage(input_columns, output_columns, pk=("id",)):
    """A valid strictly-1:1 llm_transform stage — input schema and output_schema
    share a primary_key and output ⊇ input, as Stage validation requires."""
    return parse_stage({
        "id": "evidence_extraction", "name": "Extract evidence", "type": "llm_transform",
        "inputs": [{"id": "load", "schema": {"columns": input_columns, "primary_key": list(pk)}}],
        "output_schema": {"columns": output_columns, "primary_key": list(pk)},
        "llm": {"prompt_template": "extract from {text}"},
    })


def test_llm_transform_drops_undeclared_columns_including_former_hardcoded_ids(monkeypatch):
    # The model returns benchmark_id / query_id — names the OLD hardcoded keep-list
    # would have force-kept. output_schema doesn't declare them, so they're dropped
    # (and recorded), not resurrected.
    stage = _llm_stage(
        input_columns=[{"name": "id", "type": "str", "nullable": True}, {"name": "text", "type": "str", "nullable": True}],
        output_columns=[{"name": "id", "type": "str", "nullable": True}, {"name": "text", "type": "str", "nullable": True},
                        {"name": "score", "type": "int", "nullable": False}],
    )
    monkeypatch.setattr(lt, "call_llm",
                        lambda *a, **k: {"score": 5, "benchmark_id": "B1", "query_id": "Q5"})
    ctx = make_run_context()
    out = HANDLERS[StageType.llm_transform].execute(
        stage, {"load": pd.DataFrame({"id": ["r1"], "text": ["hi"]})}, ctx)

    assert list(out.columns) == ["id", "text", "score"]
    dropped = contribution_of(out).dropped_columns
    assert "benchmark_id" in dropped and "query_id" in dropped


def test_llm_transform_declared_input_column_rides_through(monkeypatch):
    # entity_id is declared in BOTH schemas, so it's a passthrough: outside the
    # reply spec (never asked of the model) yet kept because output_schema declares
    # it. It survives by declaration, not because the runtime knows the name.
    stage = _llm_stage(
        input_columns=[{"name": "id", "type": "str", "nullable": True}, {"name": "text", "type": "str", "nullable": True},
                       {"name": "entity_id", "type": "str", "nullable": True}],
        output_columns=[{"name": "id", "type": "str", "nullable": True}, {"name": "text", "type": "str", "nullable": True},
                        {"name": "entity_id", "type": "str", "nullable": True},
                        {"name": "score", "type": "int", "nullable": False}],
    )
    monkeypatch.setattr(lt, "call_llm", lambda *a, **k: {"score": 5})
    ctx = make_run_context()
    src = pd.DataFrame({"id": ["r1"], "text": ["hi"], "entity_id": ["C:acme"]})
    out = HANDLERS[StageType.llm_transform].execute(stage, {"load": src}, ctx)

    assert list(out.columns) == ["id", "text", "entity_id", "score"]
    assert out.loc[0, "entity_id"] == "C:acme"                # rode through from input
    assert not contribution_of(out).dropped_columns           # nothing undeclared


# The columns `_src_scored()` below actually builds — what the queue stage's one
# input edge declares.
_SCORED_COLUMNS = [
    {"name": "entity_id", "type": "str", "nullable": True}, {"name": "evidence_id", "type": "str", "nullable": True},
    {"name": "quote", "type": "str", "nullable": True}, {"name": "score", "type": "int", "nullable": True},
    {"name": "benchmark_id", "type": "str", "nullable": True}, {"name": "query_id", "type": "str", "nullable": True},
]


def _queue_stage(output_schema, flt=None):
    queue = {}
    if flt is not None:
        queue["filter"] = flt
    return parse_stage({
        "id": "review", "name": "Human review", "type": "human_review_queue",
        "inputs": [{"id": "scored", "schema": {"columns": _SCORED_COLUMNS}}],
        "output_schema": output_schema,
        "queue": queue,
    })


def _src_scored():
    # filter="entity_id == 'nope'" matches nothing, so every row is a
    # pass-through row — avoids HaltForReview so the test can assert on the
    # projected output directly.
    return pd.DataFrame([
        {"entity_id": "C:acme", "evidence_id": "d1#0", "quote": "we love climate policy",
         "score": 1, "benchmark_id": "B1", "query_id": "Q5"},
    ])


def _queue_test_ctx(tmp_path, project: str) -> RunContext:
    """A production-shaped ctx for running the human_review_queue handler:
    identity + a writable stage cache, the project scope the handler's own
    guard (`_require_project_scope`) requires."""
    identity = RunIdentity(project=project, run_id="r1")
    return make_run_context(
        run_dir=tmp_path, identity=identity,
        stage_cache=StageCacheEntry.read_write(),
    )


def test_human_review_queue_keeps_only_declared_columns(tmp_path):
    stage = _queue_stage(
        output_schema={"columns": [{"name": "evidence_id", "type": "str", "nullable": True},
                                    {"name": "final_score", "type": "int", "nullable": True}]},
        flt="entity_id == 'nope'",
    )
    ctx = _queue_test_ctx(tmp_path, "keeps-declared-columns")
    out = HANDLERS[StageType.human_review_queue].execute(stage, {"scored": _src_scored()}, ctx)

    assert list(out.columns) == ["evidence_id", "final_score"]
    dropped = contribution_of(out).dropped_columns
    for col in ("entity_id", "quote", "benchmark_id", "query_id"):
        assert col in dropped


def test_human_review_queue_carried_columns_survive_by_being_declared(tmp_path):
    # `quote` survives because it's declared in output_schema, not because the
    # runtime keeps a magic list of column names.
    stage = _queue_stage(
        output_schema={"columns": [{"name": "evidence_id", "type": "str", "nullable": True},
                                    {"name": "final_score", "type": "int", "nullable": True},
                                    {"name": "quote", "type": "str", "nullable": True}]},
        flt="entity_id == 'nope'",
    )
    ctx = _queue_test_ctx(tmp_path, "carried-columns-survive")
    out = HANDLERS[StageType.human_review_queue].execute(stage, {"scored": _src_scored()}, ctx)

    assert list(out.columns) == ["evidence_id", "final_score", "quote"]
    dropped = contribution_of(out).dropped_columns
    assert "quote" not in dropped
    assert "benchmark_id" in dropped  # still dropped: not declared

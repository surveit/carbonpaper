from __future__ import annotations

import pandas as pd

from app.models import parse_stage
from app.models.stage import StageType
from app.runtime.context import RunContext, RunIdentity
from app.runtime.stages import HANDLERS
from app.runtime.stages import llm_transform as lt
from app.core.stage_cache import StageCacheEntry
from conftest import as_inputs, contribution_of, make_run_context, place_stage, queue_columns, reads_of, rows_of


def _place(stage, upstream_id, input_columns):
    return place_stage(stage, **{upstream_id: {"columns": input_columns}})


def _llm_stage(input_columns, output_columns, pk=("id",)):
    flowing = {c["name"] for c in input_columns}
    return parse_stage({
        "id": "evidence_extraction", "description": "Extract evidence", "type": "llm_transform",
        "inputs": [{"id": "load"}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": [
                c for c in input_columns if c["name"] == "text"]}],
            "adds": [c for c in output_columns if c["name"] not in flowing]},
        "llm": {"prompt_template": "extract from {text}"},
    })


def test_llm_transform_drops_undeclared_columns_including_former_hardcoded_ids(monkeypatch):
    stage = _llm_stage(
        input_columns=[{"name": "id", "type": "str", "nullable": True}, {"name": "text", "type": "str", "nullable": True}],
        output_columns=[{"name": "id", "type": "str", "nullable": True}, {"name": "text", "type": "str", "nullable": True},
                        {"name": "score", "type": "int", "nullable": False}],
    )
    monkeypatch.setattr(lt, "call_llm",
                        lambda *a, **k: {"score": 5, "benchmark_id": "B1", "query_id": "Q5"})
    ctx = make_run_context()
    out = HANDLERS[StageType.llm_transform].execute(
        _place(stage, "load", [{"name": "id", "type": "str", "nullable": True},
                               {"name": "text", "type": "str", "nullable": True}]),
        as_inputs({"load": pd.DataFrame({"id": ["r1"], "text": ["hi"]})}), ctx)

    assert list(rows_of(out).columns) == ["id", "text", "score"]
    dropped = contribution_of(out).dropped_columns
    assert "benchmark_id" in dropped and "query_id" in dropped


def test_llm_transform_declared_input_column_rides_through(monkeypatch):
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
    out = HANDLERS[StageType.llm_transform].execute(
        _place(stage, "load", [{"name": "id", "type": "str", "nullable": True},
                               {"name": "text", "type": "str", "nullable": True},
                               {"name": "entity_id", "type": "str", "nullable": True}]),
        as_inputs({"load": src}), ctx)

    assert list(rows_of(out).columns) == ["id", "text", "entity_id", "score"]
    assert rows_of(out).loc[0, "entity_id"] == "C:acme"                # rode through from input
    assert not contribution_of(out).dropped_columns           # nothing undeclared


# The columns `_src_scored()` below actually builds — what the queue stage's one
# input edge declares.
_SCORED_COLUMNS = [
    {"name": "entity_id", "type": "str", "nullable": True}, {"name": "evidence_id", "type": "str", "nullable": True},
    {"name": "quote", "type": "str", "nullable": True}, {"name": "score", "type": "int", "nullable": True},
    {"name": "benchmark_id", "type": "str", "nullable": True}, {"name": "query_id", "type": "str", "nullable": True},
]
# What `queue_columns()` names for the verdict, reviewer, timestamp and note.
# Every one must be added by the signature (app/models/stages/
# human_review_queue.py), so they are appended to whatever a test declares and
# named in its expected column list.
_REVIEW_RECORD = ["decision", "reviewer_id", "reviewed_at", "review_notes"]
_REVIEW_RECORD_COLUMNS = [
    {"name": name, "type": "str", "nullable": name != "decision"} for name in _REVIEW_RECORD
]


def _queue_stage(output_schema, flt=None):
    queue = queue_columns(source="score", target="final_score")
    # The reviewed column is named `final_score` because that is what these tests
    # declare — the runtime knows no such name of its own.
    if flt is not None:
        queue["filter"] = flt
    flowing = {c["name"] for c in _SCORED_COLUMNS}
    outputs = output_schema["columns"] + _REVIEW_RECORD_COLUMNS
    return parse_stage({
        "id": "review", "description": "Human review", "type": "human_review_queue",
        "inputs": [{"id": "scored"}],
        "signature": {"form": "extends",
                      "reads": reads_of("scored", _SCORED_COLUMNS),
                      "adds": [c for c in outputs if c["name"] not in flowing]},
        "queue": queue,
    })


def _src_scored():
    return pd.DataFrame([
        {"entity_id": "C:acme", "evidence_id": "d1#0", "quote": "we love climate policy",
         "score": 1, "benchmark_id": "B1", "query_id": "Q5"},
    ])


def _queue_test_ctx(tmp_path, project: str) -> RunContext:
    """The handler's `_require_project_scope` guard needs both an identity and a writable cache."""
    identity = RunIdentity(project=project, run_id="r1")
    return make_run_context(
        run_dir=tmp_path, identity=identity,
        stage_cache=StageCacheEntry.read_write(),
    )


def test_human_review_queue_carries_every_input_column_through(tmp_path):
    stage = _queue_stage(
        output_schema={"columns": [{"name": "evidence_id", "type": "str", "nullable": True},
                                    {"name": "final_score", "type": "int", "nullable": True}]},
        flt="entity_id == 'nope'",  # matches no row, so nothing halts
    )
    ctx = _queue_test_ctx(tmp_path, "keeps-declared-columns")
    out = HANDLERS[StageType.human_review_queue].execute(
        _place(stage, "scored", _SCORED_COLUMNS), as_inputs({"scored": _src_scored()}), ctx)

    assert list(rows_of(out).columns) == [c["name"] for c in _SCORED_COLUMNS] + [
        "final_score"] + _REVIEW_RECORD
    assert contribution_of(out).dropped_columns == []

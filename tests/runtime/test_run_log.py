"""The per-run event log's row lifecycle (app/runtime/run_log.py + stages/row_events.py).

Driven through the registered handlers: a computed row opens and settles, a
cache-answered row settles ONCE marked cached, a raising mapper is logged
before it propagates.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from app.core.stage_cache import StageCache
from app.models import parse_stage, Stage, Workflow
from app.models.stage import StageType
from app.runtime.context import RunContext, RunIdentity
from app.runtime.executor import run_subset
from app.runtime.run_log import (
    LEVEL_DETAIL,
    LEVEL_LIFECYCLE,
    LLM_PROMPT,
    RUN_DONE,
    SOURCE_CACHED,
    SOURCE_COMPUTED,
    RunLog,
    bind_detail_sink,
    emit_llm_detail,
    read_events_since,
    unbind_detail_sink,
)
from app.runtime.stages import HANDLERS
from app.runtime.stages.llm_transform import run_llm_batches
from conftest import make_run_context

PROJECT = "run-log-tests"

_DOUBLING_CODE = "def transform(row):\n    return {**row, 'y': row['x'] * 2}\n"
_RAISING_CODE = "def transform(row):\n    raise ValueError('bad row')\n"


def _row_stage(code: str = _DOUBLING_CODE) -> Stage:
    return parse_stage({
        "id": "double", "name": "Double", "type": "python_row_function",
        "inputs": [{"id": "src", "schema": {"columns": [{"name": "x", "type": "int"}]}}],
        "cache": True,
        "output_schema": {
            "columns": [{"name": "x", "type": "int"}, {"name": "y", "type": "int"}]},
        "function": {"kind": "inline", "code": code},
    })


def _llm_stage(batch_size: int) -> Stage:
    return parse_stage({
        "id": "score", "name": "Score", "type": "llm_transform",
        "inputs": [{"id": "src", "schema": {
            "columns": [{"name": "x", "type": "int"}], "primary_key": ["x"]}}],
        "output_schema": {
            "columns": [{"name": "x", "type": "int"}, {"name": "verdict", "type": "str"}],
            "primary_key": ["x"]},
        "llm": {"prompt_instructions": "score it", "prompt_data_template": "{x}",
                "batch_size": batch_size},
    })


def _logged_ctx(tmp_path: Path, run_id: str) -> tuple[RunContext, RunLog]:
    log = RunLog(tmp_path / f"{run_id}.jsonl")
    ctx = make_run_context(
        identity=RunIdentity(project=PROJECT, run_id=run_id), stage_cache=StageCache()
    ).attach_run_log(log)
    return ctx, log


def _events(path: Path, log: RunLog) -> list[dict[str, Any]]:
    log.close()
    return read_events_since(path, 0)


def _run(stage: Stage, values: list[int], ctx: RunContext) -> pd.DataFrame:
    out = HANDLERS[StageType(stage.type)].execute(
        stage, {"src": pd.DataFrame({"x": values})}, ctx
    )
    assert out is not None
    return out


def _row_events(events: list[dict[str, Any]]) -> list[tuple[str, int, Any]]:
    return [
        (e["kind"], e["row"], e.get("source")) for e in events if "row" in e
    ]


def test_a_computed_row_opens_and_settles(tmp_path):
    ctx, log = _logged_ctx(tmp_path, "computed")
    _run(_row_stage(), [1, 2], ctx)

    assert _row_events(_events(tmp_path / "computed.jsonl", log)) == [
        ("row_start", 0, None), ("row_ok", 0, SOURCE_COMPUTED),
        ("row_start", 1, None), ("row_ok", 1, SOURCE_COMPUTED),
    ]


def test_a_replayed_row_settles_once_and_is_marked_cached(tmp_path):
    stage = _row_stage()
    seed_ctx, seed_log = _logged_ctx(tmp_path, "seed")
    _run(stage, [1, 2], seed_ctx)
    seed_log.close()

    ctx, log = _logged_ctx(tmp_path, "replay")
    _run(stage, [1, 2], ctx)

    # One terminal event per row, marked cached — and NO row_start, because
    # nothing ran. A replayed row must never read as a computed one.
    assert _row_events(_events(tmp_path / "replay.jsonl", log)) == [
        ("row_ok", 0, SOURCE_CACHED), ("row_ok", 1, SOURCE_CACHED),
    ]


def test_a_raising_mapper_is_logged_before_it_propagates(tmp_path):
    ctx, log = _logged_ctx(tmp_path, "raiser")
    with pytest.raises(Exception):
        _run(_row_stage(_RAISING_CODE), [1], ctx)

    errors = [e for e in _events(tmp_path / "raiser.jsonl", log)
              if e["kind"] == "row_error"]
    assert len(errors) == 1
    assert errors[0]["row"] == 0 and errors[0]["source"] == SOURCE_COMPUTED
    assert "bad row" in errors[0]["text"]


def test_a_batched_chunk_binds_the_input_rows_it_actually_covers(tmp_path, monkeypatch):
    """The chunk's detail is attributed to the rows the chunk really is — the
    input positions the shape handed down, not offsets within the chunk."""
    def fake_call_llm_batch(stage_id, llm_config, *, instructions, task,
                            reply_schema, model=None, usage_out=None):
        emit_llm_detail(LLM_PROMPT, text=task)
        return {"results": [{"row_number": 0, "verdict": "a"},
                            {"row_number": 1, "verdict": "b"}]}

    monkeypatch.setattr(
        "app.runtime.stages.llm_transform.call_llm_batch", fake_call_llm_batch
    )
    ctx, log = _logged_ctx(tmp_path, "batched")
    rows = run_llm_batches(
        _llm_stage(batch_size=2), {"src": pd.DataFrame({"x": [7, 8]})}, ctx, 1, [3, 4]
    )

    assert [row["verdict"] for row in rows] == ["a", "b"]
    prompts = [e for e in _events(tmp_path / "batched.jsonl", log)
               if e["kind"] == LLM_PROMPT]
    assert len(prompts) == 1
    assert prompts[0]["level"] == LEVEL_DETAIL
    assert prompts[0]["row"] == 3 and prompts[0]["rows"] == [3, 4]


def test_the_batched_path_logs_replayed_and_computed_rows_apart(tmp_path, monkeypatch):
    """A batched stage whose cache answers some rows: the hits settle as cached,
    and only the misses are opened, computed, and handed to the batch driver —
    under their own input positions, not their offsets within the batch."""
    handler = HANDLERS[StageType.llm_transform]
    handed: list[list[int]] = []

    def fake_run_batches(stage, inputs, ctx, parallelism, positions):
        handed.append(list(positions))
        return [{**row, "verdict": f"v{row['x']}"}
                for row in inputs[stage.inputs[0].id].to_dict("records")]

    monkeypatch.setattr(handler, "run_batches", fake_run_batches)
    stage = _llm_stage(batch_size=2)

    seed_ctx, seed_log = _logged_ctx(tmp_path, "seed")
    handler.execute(stage, {"src": pd.DataFrame({"x": [1, 2]})}, seed_ctx)
    seed_log.close()

    ctx, log = _logged_ctx(tmp_path, "replay")
    handler.execute(stage, {"src": pd.DataFrame({"x": [1, 2, 3]})}, ctx)

    assert handed == [[0, 1], [2]]
    assert _row_events(_events(tmp_path / "replay.jsonl", log)) == [
        ("row_ok", 0, SOURCE_CACHED), ("row_ok", 1, SOURCE_CACHED),
        ("row_start", 2, None), ("row_ok", 2, SOURCE_COMPUTED),
    ]


def test_a_run_writes_its_lifecycle_spine_to_the_run_dir(tmp_path):
    """The executor opens the log for EVERY entry path — here the subset
    executor — so a run always leaves runs/<id>/events.jsonl behind, terminated
    by the run_done marker the SSE tail stops on."""
    source = parse_stage({
        "id": "src", "name": "Source", "type": "input_data",
        "connector": {"kind": "file"},
        "output_schema": {"columns": [{"name": "x", "type": "int"}]},
    })
    run_dir = tmp_path / "runs" / "subset1"
    run_subset(
        Workflow(stages=[source, _row_stage()]),
        injected_outputs={"src": pd.DataFrame({"x": [1, 2]})},
        stage_ids=["double"], run_dir=run_dir, repo_root=tmp_path,
    )

    events = read_events_since(run_dir / "events.jsonl", 0)
    assert [e["kind"] for e in events] == [
        "run_start", "stage_start",
        "row_start", "row_ok", "row_start", "row_ok",
        "stage_done", RUN_DONE,
    ]
    assert events[0]["run_id"] == "subset1" and events[0]["stage_count"] == 1
    assert events[-2]["stage"] == "double" and events[-2]["rows"] == 2
    assert [e["seq"] for e in events] == list(range(len(events)))


def _resumable_log(path: Path, stage: str) -> None:
    """Write one stage_start + the run_done marker to `path`, appending."""
    log = RunLog(path)
    log.emit({"kind": "stage_start", "stage": stage})
    log.close()


def test_a_resumed_log_keeps_seq_equal_to_the_line_index(tmp_path):
    """resume_run opens a second RunLog on the same path; seq is the file's line
    index, so it stays strictly monotonic across any number of resumes."""
    path = tmp_path / "events.jsonl"
    _resumable_log(path, "first")
    _resumable_log(path, "second")
    _resumable_log(path, "third")

    events = read_events_since(path, 0)
    assert [e["kind"] for e in events] == ["stage_start", RUN_DONE] * 3
    assert [e["seq"] for e in events] == list(range(len(events)))


def test_a_tailer_resuming_at_the_pre_resume_cursor_sees_the_resumed_events(tmp_path):
    """The consequence of a restarted seq: an SSE client filtering seq >= cursor
    would drop every event the resumed run wrote. Duplicate-free seqs alone do
    not prove this — the resumed events must actually arrive."""
    path = tmp_path / "events.jsonl"
    _resumable_log(path, "first")
    cursor = max(e["seq"] for e in read_events_since(path, 0)) + 1

    _resumable_log(path, "second")

    resumed = read_events_since(path, cursor)
    assert [e["kind"] for e in resumed] == ["stage_start", RUN_DONE]
    assert resumed[0]["stage"] == "second"


def test_an_unbound_detail_emit_is_a_no_op(tmp_path):
    log = RunLog(tmp_path / "events.jsonl")
    emit_llm_detail(LLM_PROMPT, text="nobody is listening")
    assert [e["kind"] for e in _events(tmp_path / "events.jsonl", log)] == [RUN_DONE]


def test_a_none_log_binds_no_sink(tmp_path):
    log = RunLog(tmp_path / "events.jsonl")
    token = bind_detail_sink(None, "stage", (0,))
    try:
        emit_llm_detail(LLM_PROMPT, text="dropped")
    finally:
        unbind_detail_sink(token)
    assert [e["kind"] for e in _events(tmp_path / "events.jsonl", log)] == [RUN_DONE]


def test_an_event_defaults_to_the_lifecycle_level(tmp_path):
    log = RunLog(tmp_path / "events.jsonl")
    log.emit({"kind": "stage_start", "stage": "s"})   # no level given
    events = _events(tmp_path / "events.jsonl", log)
    assert [e["kind"] for e in events] == ["stage_start", RUN_DONE]
    assert all(e["level"] == LEVEL_LIFECYCLE for e in events)

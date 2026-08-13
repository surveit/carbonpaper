from __future__ import annotations

import pandas as pd
from conftest import contribution_of, make_run_context, place_stage

from app.models import parse_stage, Stage
from app.models.stage import StageType
from app.runtime.stages import HANDLERS
from app.runtime.stages import llm_transform as lt

# Rows a,b,c at input positions 0,1,2. Expected answer for the row at position i
# is "L{i}", so a mismatch (a row given another's answer) is visible.
_SRC = pd.DataFrame({"post_id": ["a", "b", "c"], "text": ["ta", "tb", "tc"]})


def _stage(batch_size: int = 3, max_retries: int = 0) -> Stage:
    return parse_stage({
        "id": "process", "description": "Process", "type": "llm_transform",
        "inputs": [{"id": "load"}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": [
                {"name": "text", "type": "str", "nullable": True}]}],
            "adds": [{"name": "label", "type": "str", "nullable": True}]},
        # max_retries 0 → exactly one batched call, so the mock's reply is the
        # whole story (no retry masking a drop).
        "llm": {"model": "claude-haiku-4-5", "prompt_data_template": "process {text}", "batch_size": batch_size,
                "max_retries": max_retries},
    })


def _run(monkeypatch, fake, *, batch_size=3, max_retries=0, src=_SRC):
    monkeypatch.setattr(lt, "call_llm_batch", fake)
    ctx = make_run_context()
    out = HANDLERS[StageType.llm_transform].execute(place_stage(_stage(batch_size, max_retries), load={"columns": [
            {"name": "post_id", "type": "str", "nullable": True},
            {"name": "text", "type": "str", "nullable": True}]}), {"load": src.copy()}, ctx)
    # When a whole chunk fails, no row produced `label`, so the declared column is
    # absent from output (same as the 1:1 path when every row errors) — treat that
    # as "no label" per row rather than a KeyError.
    if "label" in out.columns:
        labels = {row.post_id: row.label for row in out.itertuples()}
    else:
        labels = {post_id: None for post_id in out["post_id"]}
    return out, labels, ctx


def _clean(*args, **kwargs):
    k = kwargs["task"].count("### item ")
    return {"results": [{"row_number": i, "label": f"L{i}"} for i in range(k)]}


def test_matched_by_row_number_not_reply_order(monkeypatch):
    out, labels, ctx = _run(monkeypatch, lambda *a, **k: {"results": [
        {"row_number": 2, "label": "L2"},
        {"row_number": 0, "label": "L0"},
        {"row_number": 1, "label": "L1"}]})
    assert list(out["post_id"]) == ["a", "b", "c"]         # input order preserved
    assert labels == {"a": "L0", "b": "L1", "c": "L2"}     # matched by number, not position
    assert not contribution_of(out).row_errors


def test_missing_number_fails_whole_chunk(monkeypatch):
    out, labels, ctx = _run(monkeypatch, lambda *a, **k: {"results": [
        {"row_number": 0, "label": "L0"}, {"row_number": 2, "label": "L2"}]})
    assert all(pd.isna(v) for v in labels.values())        # nothing trusted
    errors = contribution_of(out).row_errors
    assert [e["row"] for e in errors] == [0, 1, 2]         # every row flagged
    assert "missing=[1]" in errors[0]["message"]


def test_extra_unknown_number_fails_whole_chunk(monkeypatch):
    out, labels, ctx = _run(monkeypatch, lambda *a, **k: {"results": [
        {"row_number": 0, "label": "L0"}, {"row_number": 1, "label": "L1"},
        {"row_number": 2, "label": "L2"}, {"row_number": 3, "label": "Lz"}]})
    assert all(pd.isna(v) for v in labels.values())
    errors = contribution_of(out).row_errors
    assert [e["row"] for e in errors] == [0, 1, 2]
    assert "unknown=[3]" in errors[0]["message"]


def test_duplicate_number_same_length_fails_whole_chunk(monkeypatch):
    out, labels, ctx = _run(monkeypatch, lambda *a, **k: {"results": [
        {"row_number": 0, "label": "L0"}, {"row_number": 0, "label": "L0-DUP"},
        {"row_number": 2, "label": "L2"}]})
    assert all(pd.isna(v) for v in labels.values())
    msg = contribution_of(out).row_errors[0]["message"]
    assert "duplicate=[0]" in msg and "missing=[1]" in msg


def test_anomaly_is_thrown_back_and_recovers_on_retry(monkeypatch):
    replies = [
        {"results": [{"row_number": 0, "label": "L0"}, {"row_number": 2, "label": "L2"}]},
        {"results": [{"row_number": i, "label": f"L{i}"} for i in range(3)]},
    ]
    calls = {"n": 0}

    def fake(*a, **k):
        reply = replies[calls["n"]]
        calls["n"] += 1
        return reply

    out, labels, ctx = _run(monkeypatch, fake, max_retries=1)
    assert calls["n"] == 2                                 # it re-called the model
    assert labels == {"a": "L0", "b": "L1", "c": "L2"}     # recovered
    assert not contribution_of(out).row_errors


def test_grain_and_order_preserved_across_chunks(monkeypatch):
    out, labels, ctx = _run(monkeypatch, _clean, batch_size=2)
    assert list(out["post_id"]) == ["a", "b", "c"]         # count + order preserved
    assert not contribution_of(out).row_errors


def test_batched_run_reports_only_user_columns_as_dropped(monkeypatch):
    src = _SRC.assign(note=["n1", "n2", "n3"])
    out, labels, ctx = _run(monkeypatch, _clean, src=src)
    # the user column, and ONLY it
    assert contribution_of(out).dropped_columns == ["note"]
    # `_usage` rides on every batched row; the manifest must not read it as output.
    assert lt.ROW_USAGE_KEY not in out.columns


def test_batched_chunk_failure_reports_no_marker_as_a_dropped_column(monkeypatch):
    out, labels, ctx = _run(monkeypatch, lambda *a, **k: {"results": [
        {"row_number": 0, "label": "L0"}, {"row_number": 2, "label": "L2"}]})
    assert contribution_of(out).row_errors                  # the chunk did fail
    assert not contribution_of(out).dropped_columns
    assert lt.ROW_ERROR_KEY not in out.columns and lt.ROW_USAGE_KEY not in out.columns

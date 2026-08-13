from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.models import Stage, parse_stage
from app.models.stage import StageType
from app.runtime.context import RunContext, RunIdentity
from app.runtime.stage_output import StageOutput
from app.runtime.stages import HANDLERS
from app.core.stage_cache import StageCache
from conftest import as_inputs, make_run_context, place_stage, queue_columns, reads_of, rows_of

PROJECT = "hrq-declared-columns"


# The columns `_src()` builds, by declared type.
_FRAME_COLUMNS = {"id": "str", "score": "int", "label": "str"}


def _stage(queue: dict[str, object], flt: str | None = None) -> Stage:
    # Declares reviewed sources a test leaves out of the frame; an undeclared source won't parse.
    if flt is not None:
        queue = {**queue, "filter": flt}
    reviewed = queue["reviewed_columns"]
    assert isinstance(reviewed, dict)
    declared = dict(_FRAME_COLUMNS)
    declared.update({source: "str" for source in reviewed if source not in declared})
    input_columns = [{"name": name, "type": t, "nullable": True} for name, t in declared.items()]
    added = [{"name": target, "type": declared[source], "nullable": True}
             for source, target in reviewed.items()]
    added += [{"name": queue[field], "type": "str", "nullable": True}
              for field in ("verdict_column", "reviewer_column",
                            "reviewed_at_column", "review_notes_column")
              if queue.get(field) is not None]
    return parse_stage({
        "id": "review", "description": "Review", "type": "human_review_queue",
        "inputs": [{"id": "scored"}],
        "signature": {"form": "extends", "reads": reads_of("scored", input_columns),
                      "adds": added},
        "queue": queue,
    })


def _src() -> pd.DataFrame:
    return pd.DataFrame({
        "id": ["r0", "r1"], "score": [1, 2], "label": ["pos", "neg"],
    })


def _production_ctx(tmp_path: Path) -> RunContext:
    return make_run_context(
        run_dir=tmp_path,
        identity=RunIdentity(project=PROJECT, run_id="r1"),
        stage_cache=StageCache(),
    )


def _run(stage: Stage, ctx: RunContext, src: pd.DataFrame | None = None) -> StageOutput:
    out = HANDLERS[StageType.human_review_queue].execute(
        place_stage(stage), as_inputs({"scored": src if src is not None else _src()}), ctx)
    assert out is not None  # a row-mapped stage always produces a frame
    return out


# ── The filter passed the row through: verdict `skipped`, values copied ─────


def test_filtered_out_row_is_skipped_with_the_source_value_copied(tmp_path):
    # `skipped`, not `approve`: no human saw the row, and `approve` would claim one did.
    stage = _stage(queue_columns(source="score", target="human_score"), flt="id == 'nobody'")
    out = _run(stage, _production_ctx(tmp_path))

    assert list(rows_of(out)["id"]) == ["r0", "r1"]           # every row kept, in input order
    assert list(rows_of(out)["human_score"]) == [1, 2]        # copied from `score`
    assert list(rows_of(out)["decision"]) == ["skipped", "skipped"]
    assert rows_of(out)["reviewer_id"].isna().all()           # no reviewer is invented
    assert rows_of(out)["reviewed_at"].isna().all()
    assert rows_of(out)["review_notes"].isna().all()


def test_declared_names_are_the_only_columns_added(tmp_path):
    stage = _stage({
        "reviewed_columns": {"score": "checked_score"},
        "verdict_column": "review_verdict",
        "reviewer_column": "checked_by",
        "reviewed_at_column": "checked_at",
    }, flt="id == 'nobody'")
    out = _run(stage, _production_ctx(tmp_path))

    assert list(rows_of(out).columns) == [
        "id", "score", "label", "checked_score", "review_verdict",
        "checked_by", "checked_at",
    ]
    assert list(rows_of(out)["review_verdict"]) == ["skipped", "skipped"]


def test_each_reviewed_pair_maps_independently(tmp_path):
    stage = _stage({
        **queue_columns(),
        "reviewed_columns": {"score": "human_score", "label": "human_label"},
    }, flt="id == 'nobody'")
    out = _run(stage, _production_ctx(tmp_path))

    assert list(rows_of(out)["human_score"]) == [1, 2]
    assert list(rows_of(out)["human_label"]) == ["pos", "neg"]


# ── Auto-approve: same copy, verdict `approve` ─────────────────────────────


def _auto_approve_ctx(tmp_path: Path) -> RunContext:
    return RunContext.for_stages_outside_a_run(
        repo_root=tmp_path, run_dir=tmp_path, queue_auto_approve=True)


def test_auto_approve_copies_the_source_value_under_the_approve_verdict(tmp_path):
    stage = _stage(queue_columns(source="score", target="human_score"))
    out = _run(stage, _auto_approve_ctx(tmp_path))

    assert list(rows_of(out)["human_score"]) == [1, 2]
    assert list(rows_of(out)["decision"]) == ["approve", "approve"]
    assert rows_of(out)["reviewer_id"].isna().all()
    assert rows_of(out)["reviewed_at"].isna().all()


# ── A frame that does not match the declared schema fails loudly ───────────


def test_a_source_column_absent_from_the_frame_raises(tmp_path):
    stage = _stage({
        **queue_columns(), "reviewed_columns": {"confidence": "human_confidence"},
    }, flt="id == 'nobody'")

    with pytest.raises(ValueError, match="'confidence'") as exc_info:
        _run(stage, _production_ctx(tmp_path))
    assert "queue.reviewed_columns" in str(exc_info.value)
    assert "review" in str(exc_info.value)


def test_a_queued_row_also_refuses_an_absent_source_column(tmp_path):
    # With no filter every row is queued, so nothing reads `reviewed_columns` per row.
    stage = _stage({
        **queue_columns(), "reviewed_columns": {"confidence": "human_confidence"},
    })

    with pytest.raises(ValueError, match="'confidence'"):
        _run(stage, _production_ctx(tmp_path))


def test_auto_approve_also_refuses_an_absent_source_column(tmp_path):
    stage = _stage({
        **queue_columns(), "reviewed_columns": {"confidence": "human_confidence"},
    })

    with pytest.raises(ValueError, match="'confidence'"):
        _run(stage, _auto_approve_ctx(tmp_path))

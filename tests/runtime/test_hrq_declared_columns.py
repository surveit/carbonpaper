from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.models import Stage, parse_stage
from app.models.stage import StageType
from app.runtime.context import RunContext, RunIdentity
from app.runtime.stages import HANDLERS
from app.core.stage_cache import StageCache
from conftest import make_run_context, queue_columns, reads_of

PROJECT = "hrq-declared-columns"


# The columns `_src()` builds, by declared type.
_FRAME_COLUMNS = {"id": "str", "score": "int", "label": "str"}


def _stage(queue: dict[str, object], flt: str | None = None) -> Stage:
    # The input edge declares `_src()`'s columns plus any reviewed source a test
    # deliberately leaves OUT of that frame: a stage naming an undeclared source cannot be
    # built at all, and those tests are about a live frame that does not match what was
    # declared.
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
        "inputs": [{"id": "scored", "schema": {"columns": input_columns}}],
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


def _run(stage: Stage, ctx: RunContext, src: pd.DataFrame | None = None) -> pd.DataFrame:
    out = HANDLERS[StageType.human_review_queue].execute(
        stage, {"scored": src if src is not None else _src()}, ctx)
    assert out is not None  # a row-mapped stage always produces a frame
    return out


# ── The filter passed the row through: verdict `skipped`, values copied ─────


def test_filtered_out_row_is_skipped_with_the_source_value_copied(tmp_path):
    # Declaring a filter is the author's statement that the model's values stand for the
    # rows it excludes, so those values are copied into the reviewed columns. The verdict
    # is `skipped`, not `approve`: no human saw the row, and `approve` would claim one
    # did.
    stage = _stage(queue_columns(source="score", target="human_score"), flt="id == 'nobody'")
    out = _run(stage, _production_ctx(tmp_path))

    assert list(out.frame["id"]) == ["r0", "r1"]           # every row kept, in input order
    assert list(out.frame["human_score"]) == [1, 2]        # copied from `score`
    assert list(out.frame["decision"]) == ["skipped", "skipped"]
    assert out.frame["reviewer_id"].isna().all()           # no reviewer is invented
    assert out.frame["reviewed_at"].isna().all()
    assert out.frame["review_notes"].isna().all()


def test_declared_names_are_the_only_columns_added(tmp_path):
    # The added columns carry the author's names — the runtime knows no
    # `final_score`/`ai_score` vocabulary of its own.
    stage = _stage({
        "reviewed_columns": {"score": "checked_score"},
        "verdict_column": "review_verdict",
        "reviewer_column": "checked_by",
        "reviewed_at_column": "checked_at",
    }, flt="id == 'nobody'")
    out = _run(stage, _production_ctx(tmp_path))

    assert list(out.frame.columns) == [
        "id", "score", "label", "checked_score", "review_verdict",
        "checked_by", "checked_at",
    ]
    assert list(out.frame["review_verdict"]) == ["skipped", "skipped"]


def test_each_reviewed_pair_maps_independently(tmp_path):
    stage = _stage({
        **queue_columns(),
        "reviewed_columns": {"score": "human_score", "label": "human_label"},
    }, flt="id == 'nobody'")
    out = _run(stage, _production_ctx(tmp_path))

    assert list(out.frame["human_score"]) == [1, 2]
    assert list(out.frame["human_label"]) == ["pos", "neg"]


# ── Auto-approve: same copy, verdict `approve` ─────────────────────────────


def _auto_approve_ctx(tmp_path: Path) -> RunContext:
    return RunContext.for_stages_outside_a_run(
        repo_root=tmp_path, run_dir=tmp_path, queue_auto_approve=True)


def test_auto_approve_copies_the_source_value_under_the_approve_verdict(tmp_path):
    # Auto-approve is human approval's stand-in in a test run, so it keeps approve
    # semantics — but still invents no reviewer.
    stage = _stage(queue_columns(source="score", target="human_score"))
    out = _run(stage, _auto_approve_ctx(tmp_path))

    assert list(out.frame["human_score"]) == [1, 2]
    assert list(out.frame["decision"]) == ["approve", "approve"]
    assert out.frame["reviewer_id"].isna().all()
    assert out.frame["reviewed_at"].isna().all()


# ── A frame that does not match the declared schema fails loudly ───────────


def test_a_source_column_absent_from_the_frame_raises(tmp_path):
    # Authoring-time validation checks reviewed_columns against the DECLARED input schema;
    # this is the complement — a live frame that does not match it. No value may stand in
    # for the missing column.
    stage = _stage({
        **queue_columns(), "reviewed_columns": {"confidence": "human_confidence"},
    }, flt="id == 'nobody'")

    with pytest.raises(ValueError, match="'confidence'") as exc_info:
        _run(stage, _production_ctx(tmp_path))
    assert "queue.reviewed_columns" in str(exc_info.value)
    assert "review" in str(exc_info.value)


def test_a_queued_row_also_refuses_an_absent_source_column(tmp_path):
    # The path that would otherwise hide the mismatch: with no filter every row is QUEUED,
    # so no row is ever skipped or auto-approved and nothing reads `reviewed_columns` per
    # row. The stage must still refuse rather than write a review snapshot and halt for a
    # human — the frame it would hand the reviewer cannot produce the column the stage
    # declares.
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

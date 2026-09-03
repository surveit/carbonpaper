from __future__ import annotations

from collections.abc import Mapping

import pandas as pd
import pytest

from app.core.errors import ReviewValidationError
from app.core.stage_cache import StageCacheEntry
from app.models import Stage, parse_stage
from app.models.records.review_decision import ReviewDecision
from app.models.stages.human_review_queue import ReviewVerdict
from app.services import review
from conftest import place_stage, queue_columns, reads_of

FROZEN_ROW = {"id": "a", "score": 1}

# Every source any queue config below reviews, so one input schema serves them all.
_INPUT_COLUMNS: list[dict[str, object]] = [
    {"name": "id", "type": "str", "nullable": True},
    {"name": "score", "type": "int", "nullable": True},
    {"name": "label", "type": "str", "nullable": True},
]
_SOURCE_TYPES = {column["name"]: column["type"] for column in _INPUT_COLUMNS}


def _stage(queue: dict[str, object] | None = None) -> Stage:
    block = queue if queue is not None else queue_columns()
    return parse_stage({
        "id": "review", "description": "Review", "type": "human_review_queue",
        "inputs": [{"id": "scored"}],
        "signature": {"form": "extends", "reads": reads_of("scored", _INPUT_COLUMNS),
                      "adds": _added_columns(block)},
        "queue": block,
    })


def _added_columns(queue: Mapping[str, object]) -> list[dict[str, object]]:
    reviewed = queue["reviewed_columns"]
    assert isinstance(reviewed, dict)
    columns: list[dict[str, object]] = [
        {"name": target, "type": _SOURCE_TYPES[source], "nullable": True}
        for source, target in reviewed.items()
    ]
    columns += [
        {"name": queue[field], "type": "str", "nullable": True}
        for field in ("verdict_column", "reviewer_column",
                      "reviewed_at_column", "review_notes_column")
        if queue.get(field) is not None
    ]
    return columns


def _record(
    input_fingerprint: str, *,
    stage: Stage | None = None,
    verdict: ReviewVerdict = ReviewVerdict.approve,
    frozen_row: Mapping[str, object] = FROZEN_ROW,
    reviewed_values: Mapping[str, object] | None = None,
    review_notes: str | None = None,
    reviewer: str = "Ada",
    reviewed_at: str = "2026-07-22T10:00:00",
    workflow_version: str | None = None,
) -> None:
    review.record_decision(
        project_id="proj", stage=place_stage(stage if stage is not None else _stage()),
        stage_fingerprint="sf1", input_fingerprint=input_fingerprint,
        frozen_row=frozen_row, verdict=verdict,
        reviewed_values={"human_score": 1} if reviewed_values is None else reviewed_values,
        review_notes=review_notes,
        reviewer=reviewer, reviewed_at=reviewed_at,
        workflow_version=workflow_version,
    )


def _load_entry(input_fingerprint: str) -> StageCacheEntry | None:
    return StageCacheEntry.read_only().get("proj", "review", "sf1", input_fingerprint)


def _find_decisions(input_fingerprint: str) -> list[ReviewDecision]:
    return ReviewDecision.find(
        project="proj", stage_id="review", stage_fingerprint="sf1",
        input_fingerprint=input_fingerprint,
    )


# ── The output row a verdict produces ───────────────────────────────────────


def test_approve_records_the_reviewed_values_under_the_declared_columns():
    _record("if1")

    entry = _load_entry("if1")
    assert entry is not None
    assert entry.output_row == {
        "id": "a", "score": 1,          # the frozen input carried through
        "human_score": 1,               # queue.reviewed_columns' target
        "decision": "approve",          # queue.verdict_column
        "reviewer_id": "Ada",           # queue.reviewer_column
        "reviewed_at": "2026-07-22T10:00:00",
        "review_notes": None,           # declared, and the reviewer wrote none
    }


def test_modify_records_the_value_the_reviewer_entered_not_the_ai_value():
    _record("if2", verdict=ReviewVerdict.modify, reviewed_values={"human_score": 42.0})

    entry = _load_entry("if2")
    assert entry is not None
    assert entry.output_row is not None
    assert entry.output_row["human_score"] == 42.0
    assert entry.output_row["score"] == 1  # the AI value stays on the row
    assert entry.output_row["decision"] == "modify"


def test_every_declared_pair_lands_under_its_own_target_column():
    queue = {**queue_columns(), "reviewed_columns": {"score": "checked_score",
                                                     "label": "checked_label"}}
    _record(
        "if3", stage=_stage(queue),
        frozen_row={"id": "a", "score": 1, "label": "pos"},
        reviewed_values={"checked_score": 2, "checked_label": "neg"},
    )

    entry = _load_entry("if3")
    assert entry is not None
    assert entry.output_row is not None
    assert entry.output_row["checked_score"] == 2
    assert entry.output_row["checked_label"] == "neg"


def test_notes_the_reviewer_wrote_land_in_the_declared_notes_column():
    _record("if4", review_notes="the model missed the hedge")

    entry = _load_entry("if4")
    assert entry is not None
    assert entry.output_row is not None
    assert entry.output_row["review_notes"] == "the model missed the hedge"


def test_a_config_with_no_notes_column_adds_no_notes_column():
    queue = {**queue_columns(), "review_notes_column": None}
    _record("if5", stage=_stage(queue))

    entry = _load_entry("if5")
    assert entry is not None
    assert entry.output_row is not None
    assert "review_notes" not in entry.output_row


# ── The three domain rules ──────────────────────────────────────────────────


def test_rejects_the_runtime_only_skipped_verdict():
    with pytest.raises(ReviewValidationError, match="skipped"):
        _record("if6", verdict=ReviewVerdict.skipped)
    assert _load_entry("if6") is None


def test_rejects_reviewed_values_missing_a_declared_column():
    queue = {**queue_columns(), "reviewed_columns": {"score": "checked_score",
                                                     "label": "checked_label"}}
    with pytest.raises(ReviewValidationError, match="checked_label"):
        _record("if7", stage=_stage(queue), reviewed_values={"checked_score": 2})
    assert _load_entry("if7") is None


def test_rejects_a_reviewed_value_for_an_undeclared_column():
    with pytest.raises(ReviewValidationError, match="made_up"):
        _record("if8", reviewed_values={"human_score": 1, "made_up": 9})
    assert _load_entry("if8") is None


def test_rejects_notes_when_no_notes_column_is_declared():
    queue = {**queue_columns(), "review_notes_column": None}
    with pytest.raises(ReviewValidationError, match="review_notes_column"):
        _record("if9", stage=_stage(queue), review_notes="a note with nowhere to go")
    assert _load_entry("if9") is None


# ── The ledger: append-only, independent of the cache ───────────────────────


def test_a_decision_appends_one_ledger_row_carrying_everything_recorded():
    _record(
        "if10", verdict=ReviewVerdict.modify, reviewed_values={"human_score": 7},
        review_notes="looked low", reviewer="Grace", reviewed_at="2026-08-01T09:00:00",
        workflow_version="v3",
    )

    (decision,) = _find_decisions("if10")
    assert decision.project == "proj"
    assert decision.stage_id == "review"
    assert decision.stage_fingerprint == "sf1"
    assert decision.input_fingerprint == "if10"
    assert decision.frozen_input == FROZEN_ROW
    assert decision.verdict == ReviewVerdict.modify
    assert decision.reviewed_values == {"human_score": 7}
    assert decision.review_notes == "looked low"
    assert decision.reviewer == "Grace"
    assert decision.reviewed_at == "2026-08-01T09:00:00"
    assert decision.workflow_version == "v3"


def test_re_deciding_the_same_row_appends_a_second_row_and_leaves_the_first_alone():
    """Today's `StageCacheEntry.record` overwrites in place — the ledger must not."""
    _record("if11", verdict=ReviewVerdict.approve, reviewed_values={"human_score": 1})
    _record("if11", verdict=ReviewVerdict.modify, reviewed_values={"human_score": 9})

    decisions = _find_decisions("if11")
    assert len(decisions) == 2
    verdicts_by_id = {decision.id: decision.verdict for decision in decisions}
    values_by_id = {decision.id: decision.reviewed_values for decision in decisions}
    assert sorted(verdicts_by_id.values()) == sorted(
        [ReviewVerdict.approve, ReviewVerdict.modify]
    )
    assert sorted(values_by_id.values(), key=str) == sorted(
        [{"human_score": 1}, {"human_score": 9}], key=str
    )
    # StageCacheEntry.record still overwrites: the cache holds only the latest row.
    entry = _load_entry("if11")
    assert entry is not None
    assert entry.output_row is not None
    assert entry.output_row["human_score"] == 9


def test_latest_wins_by_created_at_even_when_reviewed_at_disagrees():
    """`reviewed_at` is caller-supplied; a later append with an EARLIER `reviewed_at` must still win."""
    _record(
        "if12", verdict=ReviewVerdict.approve, reviewed_values={"human_score": 1},
        reviewed_at="2026-09-01T00:00:00",
    )
    _record(
        "if12", verdict=ReviewVerdict.modify, reviewed_values={"human_score": 2},
        reviewed_at="2020-01-01T00:00:00",
    )

    latest = review.find_latest_decision(
        project_id="proj", stage_id="review", stage_fingerprint="sf1", input_fingerprint="if12",
    )
    assert latest is not None
    assert latest.verdict == ReviewVerdict.modify
    assert latest.reviewed_values == {"human_score": 2}


def test_a_value_read_straight_off_a_frame_reaches_both_stores():
    """A pandas cell is `numpy.int64`; unnormalised it serialized into the cache and not the ledger."""
    scored = pd.DataFrame({"id": ["a"], "score": [4]})

    _record("if13", reviewed_values={"human_score": scored["score"].iloc[0]})

    decisions = _find_decisions("if13")
    assert len(decisions) == 1
    assert decisions[0].reviewed_values == {"human_score": 4}
    entry = _load_entry("if13")
    assert entry is not None and entry.output_row is not None
    assert entry.output_row["human_score"] == 4


def test_find_latest_decision_returns_none_for_an_undecided_row():
    assert review.find_latest_decision(
        project_id="proj", stage_id="review", stage_fingerprint="sf1", input_fingerprint="never-decided",
    ) is None

"""The stages a figure came through are the ones upstream of its stage."""
from __future__ import annotations

import pytest

from app.models import parse_stage
from app.models.run_manifest import RowsPendingReview, find_rows_pending_review
from app.models.workflow import find_stages_upstream_of

_ROWS = [{"name": "amount", "type": "float", "nullable": False}]


def _load(stage_id: str) -> dict:
    return {"id": stage_id, "description": "Loads rows", "type": "input_data",
            "connector": {"kind": "file"},
            "signature": {"form": "replaces", "produces": _ROWS}}


def _passthrough(stage_id: str, *inputs: str) -> dict:
    return {"id": stage_id, "description": "Keeps every row", "type": "filter_rows",
            "inputs": [{"id": i} for i in inputs],
            "filter": {"predicate": "pass this step's test", "summary": "Keeps all.", "corner_cases": [],
                       "code": "def should_include(row):\n    return True\n"},
            "signature": {"form": "extends",
                          "reads": [{"input": inputs[0], "columns": _ROWS}],
                          "adds": [], "rewrites": []}}


def test_the_walk_climbs_every_input_and_stops_at_the_loads():
    stages = [parse_stage(s) for s in (
        _load("east"), _load("west"), _load("lookup"),
        _passthrough("both", "east"), _passthrough("tagged", "both"),
        _passthrough("aside", "lookup"))]

    assert find_stages_upstream_of(stages, "tagged") == {"both", "east"}
    assert find_stages_upstream_of(stages, "east") == set()


_QUEUE_AND_BRANCHES = (
    _load("east"), _passthrough("review", "east"), _passthrough("totals", "review"),
    _passthrough("aside", "east"))
_REVIEW_WAITING = [{"stage_id": "review", "status": "awaiting_review"}]


def test_a_queues_pending_rows_reach_every_stage_behind_it_and_no_other():
    stages = [parse_stage(s) for s in _QUEUE_AND_BRANCHES]
    manifest = {
        "stage_records": _REVIEW_WAITING,
        "human_review_queue_stats": {"review": {"items_pending": 3}},
    }

    assert find_rows_pending_review(manifest, stages) == {
        "review": RowsPendingReview(count=3), "totals": RowsPendingReview(count=3)}


def test_a_queue_that_recorded_no_count_still_holds_its_stages_with_no_number():
    stages = [parse_stage(s) for s in _QUEUE_AND_BRANCHES]
    manifest = {"stage_records": _REVIEW_WAITING, "human_review_queue_stats": {}}

    assert find_rows_pending_review(manifest, stages) == {
        "review": RowsPendingReview(count=None), "totals": RowsPendingReview(count=None)}


def test_an_unknown_stage_is_refused_by_name():
    with pytest.raises(ValueError, match="nowhere"):
        find_stages_upstream_of([parse_stage(_load("east"))], "nowhere")


def test_an_input_no_stage_resolves_stops_the_walk():
    stages = [parse_stage(s) for s in (_load("east"), _passthrough("orphan", "vanished"))]

    with pytest.raises(ValueError, match="vanished"):
        find_stages_upstream_of(stages, "orphan")

"""`queue.sort` orders the queue SNAPSHOT, and the sidecar must be permuted with it."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app.core.stage_cache import StageCache
from app.models import Stage, parse_stage
from app.models.stage import StageType
from app.runtime.context import RunIdentity
from app.runtime.errors import HaltForReview
from app.runtime.stages import HANDLERS
from conftest import (
    QUEUE_COLUMNS, make_run_context, place_stage, queue_added_columns, reads_of,
)

PROJECT = "hrq-sort-tests"

_COLUMNS = [
    {"name": "id", "type": "str", "nullable": False},
    {"name": "score", "type": "int", "nullable": True},
]
_DATED_COLUMNS = [*_COLUMNS, {"name": "filed_on", "type": "date", "nullable": True}]


def _stage(sort: list[dict[str, str]] | None = None, columns=_COLUMNS) -> Stage:
    queue: dict[str, object] = dict(QUEUE_COLUMNS)
    if sort is not None:
        queue["sort"] = sort
    return parse_stage({
        "id": "review", "description": "Review", "type": "human_review_queue",
        "inputs": [{"id": "scored"}],
        "signature": {"form": "extends", "reads": reads_of("scored", columns),
                      "adds": queue_added_columns()},
        "queue": queue,
    })


def _halt(stage: Stage, frame: pd.DataFrame, tmp_path, run_id: str):
    ctx = make_run_context(
        run_dir=tmp_path / run_id,
        identity=RunIdentity(project=PROJECT, run_id=run_id),
        stage_cache=StageCache(),
    )
    with pytest.raises(HaltForReview) as exc_info:
        HANDLERS[StageType.human_review_queue].execute(
            place_stage(stage), {"scored": frame}, ctx
        )
    queue_path = exc_info.value.queue_path
    sidecar = queue_path.parent / f"{queue_path.stem}.fingerprints.json"
    return pd.read_parquet(queue_path), json.loads(sidecar.read_text(encoding="utf-8"))


def _scored(ids: list[str], scores: list[int | None]) -> pd.DataFrame:
    return pd.DataFrame({"id": ids, "score": pd.array(scores, dtype="Int64")})


def _by(column: str, direction: str) -> dict[str, str]:
    return {"column": column, "direction": direction}


# ── the order itself ─────────────────────────────────────────────────────────


def test_an_undeclared_sort_leaves_the_queue_in_upstream_order(tmp_path):
    snapshot, _ = _halt(_stage(), _scored(["a", "b", "c"], [1, 9, 5]), tmp_path, "none")
    assert list(snapshot["id"]) == ["a", "b", "c"]


def test_descending_puts_the_largest_value_first(tmp_path):
    stage = _stage([_by("score", "descending")])
    snapshot, _ = _halt(stage, _scored(["a", "b", "c"], [1, 9, 5]), tmp_path, "desc")
    assert list(snapshot["id"]) == ["b", "c", "a"]


def test_ascending_puts_the_smallest_value_first(tmp_path):
    stage = _stage([_by("score", "ascending")])
    snapshot, _ = _halt(stage, _scored(["a", "b", "c"], [1, 9, 5]), tmp_path, "asc")
    assert list(snapshot["id"]) == ["a", "c", "b"]


def test_a_second_key_breaks_the_first_keys_ties(tmp_path):
    frame = pd.DataFrame({
        "id": ["a", "b", "c"],
        "score": pd.array([5, 5, 9], dtype="Int64"),
        "filed_on": pd.to_datetime(["2026-03-01", "2026-01-01", "2026-02-01"]),
    })
    stage = _stage([_by("score", "descending"), _by("filed_on", "ascending")],
                   columns=_DATED_COLUMNS)
    snapshot, _ = _halt(stage, frame, tmp_path, "two-keys")
    assert list(snapshot["id"]) == ["c", "b", "a"]


def test_rows_equal_on_every_key_keep_their_upstream_order(tmp_path):
    stage = _stage([_by("score", "descending")])
    snapshot, _ = _halt(stage, _scored(["a", "b", "c", "d"], [7, 7, 7, 7]), tmp_path, "ties")
    assert list(snapshot["id"]) == ["a", "b", "c", "d"]


# ── nulls ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("direction", ["ascending", "descending"])
def test_a_null_sorts_last_in_either_direction(direction, tmp_path):
    # A null is an absent value, not a small one: it must never lead the queue.
    stage = _stage([_by("score", direction)])
    snapshot, _ = _halt(stage, _scored(["a", "b", "c"], [None, 9, 5]),
                        tmp_path, f"nulls-{direction}")
    assert list(snapshot["id"])[-1] == "a"


# ── the alignment the sort must not break ────────────────────────────────────


def test_every_row_keeps_its_own_fingerprint_and_row_ordinal(tmp_path):
    frame = _scored(["a", "b", "c"], [1, 9, 5])
    # The sidecar is POSITIONALLY aligned to the snapshot, so permuting the rows
    # alone would show every card another row's decision and lineage link.
    _, unsorted = _halt(_stage(), frame, tmp_path, "pairing-unsorted")
    fingerprint_by_id = dict(zip(["a", "b", "c"], unsorted["input_fingerprints"]))

    snapshot, sidecar = _halt(
        _stage([_by("score", "descending")]), frame, tmp_path, "pairing-sorted"
    )
    assert list(snapshot["id"]) == ["b", "c", "a"]
    assert sidecar["input_fingerprints"] == [fingerprint_by_id[i] for i in ["b", "c", "a"]]
    # The ordinal is the row's position in the INPUT frame, which the sort moves.
    assert sidecar["row_ordinals"] == [1, 2, 0]


def test_the_stage_fingerprint_does_not_move_when_only_the_sort_changes():
    # `sort` is INCIDENTAL: reordering the queue must not re-ask a decided row.
    assert (
        _stage().compute_definition_fingerprint()
        == _stage([_by("score", "descending")]).compute_definition_fingerprint()
    )


# ── the column that is not there at runtime ──────────────────────────────────


def test_a_sort_column_missing_from_the_frame_raises_and_names_it(tmp_path):
    stage = _stage([_by("filed_on", "descending")], columns=_DATED_COLUMNS)
    # The edge declares `filed_on`, the frame that arrives does not carry it —
    # sorting around it would silently review the rows in some other order.
    with pytest.raises(ValueError) as excinfo:
        _halt(stage, _scored(["a", "b"], [1, 2]), tmp_path, "absent")
    message = str(excinfo.value)
    assert "human_review_queue 'review'" in message
    assert "queue.sort" in message and "filed_on" in message

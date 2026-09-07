"""A store written before a rename still loads, and keeps the keys it was written under."""
from __future__ import annotations

from app.models.records.run_manifest import RunManifest
from app.models.retired_names import RETIRED_QUEUE_STATS_KEY
from app.models.stage import parse_stage
from app.models.stages.stage_base import StageType
from conftest import queue_added_columns, queue_columns, reads_of

_COLUMNS = [{"name": "id", "type": "str", "nullable": True}]
_STATS = {"items_queued_total": 2, "items_passed_through": 0,
          "items_pending": 1, "items_decided": 1}


def _queue_spec(stage_type: str) -> dict:
    return {
        "id": "gate", "description": "Review each row", "type": stage_type,
        "inputs": [{"id": "load"}],
        "queue": {**queue_columns(), "reviewer_instructions": "Confirm each row."},
        "signature": {"form": "extends", "reads": reads_of("load", _COLUMNS),
                      "adds": queue_added_columns()},
    }


def test_a_stage_stored_under_the_retired_type_name_loads_as_the_live_one() -> None:
    assert parse_stage(_queue_spec("human_review_queue")).type == StageType.review_queue


def test_both_spellings_fingerprint_the_same_so_no_cached_row_is_orphaned() -> None:
    """The type is hashed into the fingerprint the stage cache and decision ledger key on."""
    stored = parse_stage(_queue_spec("human_review_queue"))
    live = parse_stage(_queue_spec("review_queue"))

    assert stored.compute_definition_fingerprint() == live.compute_definition_fingerprint()


def test_the_enum_reads_a_retired_name_wherever_a_bare_stage_type_is_stored() -> None:
    """A run's `stage_records[].type` is one, and no discriminated union covers it."""
    assert StageType("human_review_queue") is StageType.review_queue


def test_a_manifest_written_before_the_rename_still_reports_its_queue_counts() -> None:
    manifest = RunManifest.model_validate({
        "run_id": "r1", "project": "proj", "workflow_version": "v1",
        "status": "awaiting_review", "started_at": "2026-09-07T00:00:00",
        RETIRED_QUEUE_STATS_KEY: {"gate": _STATS},
        "stage_records": [],
    })

    assert manifest.review_queue_stats == {"gate": _STATS}

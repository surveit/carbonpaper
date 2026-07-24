"""The typed `RunManifest` reproduces the run manifest's historical on-disk JSON.

The `tests/goldens/*.json` fixtures are real `manifest.json` files captured from
the pre-typing dict code for three representative runs — a clean run
(`ok_run`), an errored chain with a blocked-`pending` tail (`errored_run`), and a
halted run carrying `queue_stats` plus a blocked tail (`halted_run`). Parsing
each through `RunManifest` and re-serializing must preserve every key, value, and
optional-field omission the dict code produced.

Byte-for-byte identity holds for a fully-settled manifest (`ok_run`): the model's
field order matches the historical insertion order of a stage that RAN. A
manifest that still contains a never-started `pending` stage record differs only
in the intra-record position of `started_at` (the old code built pending and
started records with two different literal key orders; one canonical model order
cannot reproduce both) — so those assert structural equality, which proves the
keys/values/omission are identical.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.run_status import RunStatus, StageStatus
from app.models import Stage
from app.runtime.manifest import (
    RunManifest,
    StageContribution,
    create_run_manifest,
)

GOLDENS = Path(__file__).parent / "goldens"


def _golden(name: str) -> str:
    return (GOLDENS / f"{name}.json").read_text(encoding="utf-8")


def _reserialize(raw: str) -> str:
    return json.dumps(RunManifest.model_validate_json(raw).to_dict(), indent=2, default=str)


def test_fully_settled_manifest_round_trips_byte_identical():
    """A run whose every stage settled (`ok_run`) serializes back to the exact
    bytes the dict code wrote — same keys, same order, same values."""
    raw = _golden("ok_run")
    assert _reserialize(raw) == raw


@pytest.mark.parametrize("name", ["ok_run", "errored_run", "halted_run"])
def test_manifest_round_trips_structurally(name: str):
    """Every representative manifest — clean, errored-with-pending-tail, and
    halted-with-queue-stats — re-serializes to the same key/value structure and
    the same set of present-vs-omitted optional fields."""
    raw = _golden(name)
    assert json.loads(_reserialize(raw)) == json.loads(raw)


def test_optional_fields_are_omitted_exactly_where_the_dict_code_omitted_them():
    """Per-stage optional fields appear only on the records that earned them: a
    ran stage carries `output_path` but no `queue_path`/`llm_usage`; a halted
    queue stage carries `queue_path` but no `output_path`; a never-started
    pending stage carries none of the four optionals."""
    halted = RunManifest.model_validate_json(_golden("halted_run"))
    by_id = {r.stage_id: r for r in halted.stages}

    load = by_id["load"].model_dump(exclude_unset=True)
    assert "output_path" in load and "queue_path" not in load and "llm_usage" not in load

    review = by_id["review"].model_dump(exclude_unset=True)
    assert "queue_path" in review and "output_path" not in review

    tail = by_id["tail"].model_dump(exclude_unset=True)
    assert not ({"output_path", "queue_path", "notes", "llm_usage"} & set(tail))


def test_minted_manifest_omits_the_run_level_optionals():
    """A freshly-minted manifest is all-pending and carries none of the
    run-level optionals the run only earns later (`finished_at`, `halted_at`,
    `cancelled_at`, `resumed_at`, `updated_at`)."""
    abs_path = str((Path.cwd() / "x.csv").resolve())
    stage = Stage.model_validate(
        {"id": "s", "name": "S", "type": "input_data",
         "connector": {"kind": "file", "params": {"path": abs_path, "format": "csv"}}}
    )
    manifest = create_run_manifest(
        [stage], run_id="r", project="p", workflow_version="v",
        run_bindings={}, input_bindings={}, limits={}, offsets={})
    dumped = manifest.to_dict()

    assert dumped["status"] == RunStatus.RUNNING
    assert dumped["stages"][0]["status"] == StageStatus.PENDING
    for absent in ("finished_at", "halted_at", "cancelled_at", "resumed_at", "updated_at"):
        assert absent not in dumped
    # The always-present core fields ARE emitted even when empty.
    for present in ("queue_stats", "dropped_columns", "limit_overrides"):
        assert present in dumped


def test_legacy_scalar_halted_at_is_normalized_to_a_list():
    """A pre-fork-aware manifest that persisted `halted_at` as a bare stage-id
    string parses into a one-element list, so no template iterates it
    character-by-character."""
    raw = json.loads(_golden("halted_run"))
    raw["halted_at"] = "review"
    manifest = RunManifest.model_validate(raw)
    assert manifest.halted_at == ["review"]
    assert manifest.to_dict()["halted_at"] == ["review"]


def test_unset_drops_an_optional_run_field_from_serialization():
    """`unset` clears an optional run-level field so `exclude_unset` omits it —
    the model equivalent of the dict code's `manifest.pop('halted_at', None)` on
    resume."""
    manifest = RunManifest.model_validate_json(_golden("halted_run"))
    assert "halted_at" in manifest.to_dict()
    manifest.unset("halted_at")
    assert "halted_at" not in manifest.to_dict()


def test_recorded_tallies_survive_serialization_on_a_partial_manifest():
    """A resumed legacy manifest that reached this run WITHOUT `queue_stats`
    still emits a tally recorded mid-run: `record_queue_stats` marks the field
    set so `exclude_unset` keeps it (an in-place dict mutation alone would be
    dropped)."""
    manifest = RunManifest(
        run_id="r", started_at="t", project="p", workflow_version="v",
        status=RunStatus.RUNNING, stages=[])
    # queue_stats/dropped_columns defaulted, NOT in the set-fields yet.
    assert "queue_stats" not in manifest.to_dict()
    manifest.record_queue_stats("review", {
        "items_queued_total": 1, "items_passed_through": 0,
        "items_pending": 1, "items_decided": 0})
    dumped = manifest.to_dict()
    assert dumped["queue_stats"]["review"]["items_pending"] == 1


def test_empty_contribution_is_the_default():
    """A stage that contributes nothing yields an empty StageContribution — no
    usage, no errors, no drops, no queue stats."""
    empty = StageContribution()
    assert empty.llm_usage is None and empty.queue_stats is None
    assert empty.row_errors == [] and empty.dropped_columns == []

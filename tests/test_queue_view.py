# The queue page's view model, built from declared schemas alone — no run, no
# HTTP round-trip. The route-level surface lives in tests/test_review_routes.py.
from __future__ import annotations

import pandas as pd
import pytest

from fastapi import HTTPException

from app.models import Stage, parse_stage
from app.web import queue_view
from app.web.loading import QueueFingerprints
from conftest import queue_added_columns, queue_columns, reads_of


def _queue_stage(
    input_columns: list[dict[str, object]],
    *,
    source: str = "label",
    target: str = "human_label",
    target_type: str = "str",
    target_spec: dict[str, object] | None = None,
    input_ids: list[str] | None = None,
    reads: list[str] | None = None,
) -> Stage:
    added: list[dict[str, object]] = queue_added_columns(target, target_type)
    added[0] = {**added[0], **(target_spec or {})}
    upstream_ids = input_ids or ["upstream"]
    inputs = [
        {"id": upstream, "schema": {"columns": input_columns}}
        for upstream in upstream_ids
    ]
    read_columns = (input_columns if reads is None
                    else [c for c in input_columns if c["name"] in reads])
    signature: dict[str, object] = {
        "form": "extends", "adds": added,
        "reads": reads_of(upstream_ids[0], read_columns),
    }
    return parse_stage({
        "id": "review", "description": "Review", "type": "human_review_queue",
        "inputs": inputs,
        "signature": signature,
        "queue": queue_columns(source=source, target=target),
    })


_LABEL_COLUMNS: list[dict[str, object]] = [
    {"name": "id", "type": "str", "nullable": True},
    {"name": "score", "type": "int", "description": "the score this row was labelled from", "nullable": True},
    {"name": "label", "type": "str", "description": "high when the score exceeds one", "nullable": True},
]


# ── Lineage: the upstream stage's row, or a stated reason for no link ────────


def test_lineage_links_the_single_upstream_stage_at_the_sidecar_ordinal():
    # At halt the queue stage has no output, so the link names the upstream stage and ordinal.
    stage = _queue_stage(_LABEL_COLUMNS, input_ids=["label"])
    fingerprints = QueueFingerprints("sf", ["fp0", "fp1"], [3, 7])

    lineage = queue_view.resolve_lineage(stage, fingerprints)

    assert lineage == queue_view.Lineage("label", None)
    assert queue_view.build_lineage_urls("proj", "run1", lineage, fingerprints) == [
        "/project/proj/runs/run1/stage/label/row/3/trace/view",
        "/project/proj/runs/run1/stage/label/row/7/trace/view",
    ]


def test_lineage_states_why_no_link_can_be_built():
    # Halted before ordinals were recorded; a 2-input queue stage no longer parses.
    stage = _queue_stage(_LABEL_COLUMNS, input_ids=["label"])
    fingerprints = QueueFingerprints("sf", ["fp0", "fp1"], None)
    expected_in_note = "ordinal"

    lineage = queue_view.resolve_lineage(stage, fingerprints)

    assert lineage.upstream_stage_id is None
    assert expected_in_note in (lineage.note or "")
    assert queue_view.build_lineage_urls("proj", "run1", lineage, fingerprints) == [None, None]


# ── Describing the queued rows from the declared input schema ────────────────


def test_queued_columns_carry_the_declared_description():
    stage = _queue_stage(_LABEL_COLUMNS)
    snapshot = pd.DataFrame({"id": ["a"], "score": [2], "label": ["high"]})

    described = queue_view.describe_queued_columns(stage, snapshot)

    by_name = {column.name: column for column in described.columns}
    assert by_name["label"].description == "high when the score exceeds one"
    assert by_name["id"].description is None
    assert described.schema_note is None


def test_a_schema_and_snapshot_that_disagree_are_reported_not_papered_over():
    stage = _queue_stage(_LABEL_COLUMNS)
    snapshot = pd.DataFrame({"id": ["a"], "label": ["high"], "extra": [1]})

    described = queue_view.describe_queued_columns(stage, snapshot)

    note = described.schema_note or ""
    assert "'score'" in note and "'extra'" in note
    assert described.columns[-1].description is None  # undeclared: no invented prose


def test_the_context_table_omits_the_columns_under_review():
    stage = _queue_stage(_LABEL_COLUMNS)
    snapshot = pd.DataFrame({"id": ["a"], "score": [2], "label": ["high"]})

    page = queue_view.build_queue_page("p", "r", stage, stage.queue, snapshot, None, None)

    assert [column.name for column in page.context_columns] == ["id", "score"]


# ── The reviewed fields ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "target_spec, expected",
    [
        ({"description": "the label after review"}, "the label after review"),  # the TARGET's
        ({}, "high when the score exceeds one"),                     # else the SOURCE column's
    ],
)
def test_a_reviewed_field_describes_itself_from_the_target_then_the_source(
    target_spec, expected
):
    stage = _queue_stage(_LABEL_COLUMNS, target_spec=target_spec)

    field, = queue_view.build_reviewed_fields(stage, stage.queue)

    assert (field.source, field.target) == ("label", "human_label")
    assert field.description == expected


@pytest.mark.parametrize(
    "target_type, control, options, step",
    [
        ("str", "text", None, None),
        ("int", "number", None, "1"),
        ("float", "number", None, "any"),
        # Three states, not two: a checkbox would advertise a missing value as
        # `false` and an untouched submit would record it.
        ("bool", "select", ["true", "false"], None),
        ("date", "date", None, None),
        ("datetime", "datetime-local", None, None),
    ],
)
def test_a_reviewed_field_takes_its_control_from_the_declared_type(
    target_type, control, options, step
):
    stage = _queue_stage(
        [{"name": "id", "type": "str", "nullable": True}, {"name": "label", "type": target_type, "nullable": True}],
        target_type=target_type,
    )

    field, = queue_view.build_reviewed_fields(stage, stage.queue)

    assert (field.control, field.options, field.step) == (control, options, step)


def test_a_declared_range_becomes_the_fields_bounds():
    stage = _queue_stage(
        [{"name": "id", "type": "str", "nullable": True}, {"name": "label", "type": "int", "range": [0, 5], "nullable": True}],
        target_type="int", target_spec={"range": [0, 5]},
    )

    field, = queue_view.build_reviewed_fields(stage, stage.queue)

    assert (field.minimum, field.maximum) == (0, 5)


def test_the_notes_label_prefers_the_declared_description():
    stage = _queue_stage(_LABEL_COLUMNS)
    assert queue_view.resolve_notes_label(stage, "review_notes") == "Review notes"
    assert queue_view.resolve_notes_label(stage, "reviewer_notes") == "Reviewer notes"

    described = stage.model_copy(update={"signature": stage.signature.model_copy(
        update={"adds": [
            column.model_copy(update={"description": "Why you decided as you did"})
            if column.name == "review_notes" else column
            for column in stage.signature.adds]})})
    assert queue_view.resolve_notes_label(described, "review_notes") == (
        "Why you decided as you did")


# ── The value a control opens on ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "target_type, value, expected",
    [
        # A select's prefill is spelled the way its options are, so the option
        # comes back SELECTED; a value matching no option is an explicit unset,
        # never the first option the browser would otherwise show.
        ("bool", True, "true"),
        ("bool", False, "false"),
        ("bool", "", None),
        ("bool", None, None),
        # A recorded temporal value comes back from the cache stringified and
        # space-separated, which date/datetime-local controls render as blank.
        ("datetime", "2026-03-04 09:30:00", "2026-03-04T09:30:00"),
        ("date", "2026-03-04 00:00:00", "2026-03-04"),
        ("int", 7, 7),
    ],
)
def test_a_control_opens_on_the_value_in_its_own_spelling(target_type, value, expected):
    stage = _queue_stage(
        [{"name": "id", "type": "str", "nullable": True},
         {"name": "label", "type": target_type, "nullable": True}],
        target_type=target_type, target_spec={"nullable": True},
    )
    field, = queue_view.build_reviewed_fields(stage, stage.queue)

    assert queue_view._resolve_prefill(field, value) == expected


def test_an_enum_prefill_keeps_a_declared_value_and_drops_an_undeclared_one():
    stage = _queue_stage(
        [{"name": "id", "type": "str", "nullable": True},
         {"name": "label", "type": "str", "enum": ["yes", "no", "unclear"], "nullable": True}],
        target_spec={"enum": ["yes", "no", "unclear"]},
    )

    field, = queue_view.build_reviewed_fields(stage, stage.queue)

    assert field.control == "select" and field.options == ["yes", "no", "unclear"]
    assert queue_view._resolve_prefill(field, "unclear") == "unclear"
    assert queue_view._resolve_prefill(field, "retired") is None


# ── Finding the item behind one card, by the fingerprint the card carries ────


def _page_of(*input_fingerprints: str) -> queue_view.QueuePage:
    return queue_view.QueuePage(
        reviewed_fields=[], review_notes_label=None, context_columns=[],
        schema_note=None, lineage_note=None,
        items=[
            queue_view.ReviewItem(
                input_fingerprint=fingerprint, row={}, lineage_url=None,
                prior_decision=None, prefill={}, upstream_text={},
            )
            for fingerprint in input_fingerprints
        ],
        reviewed_count=0, total=len(input_fingerprints), all_reviewed=False,
    )


def test_an_item_is_found_at_the_position_its_own_card_states():
    found = queue_view.find_positioned_item(_page_of("fp0", "fp1", "fp2"), "fp1")

    assert found is not None and found.item.input_fingerprint == "fp1"
    assert found.row_position == 2  # 1-based: the card reads "Row 2 of 3"


def test_no_item_is_found_for_a_fingerprint_the_queue_does_not_carry():
    assert queue_view.find_positioned_item(_page_of("fp0"), "fp9") is None


def test_every_recorded_verdict_has_a_past_tense_label():
    from app.models.stages.human_review_queue import ReviewVerdict
    from app.web.queue_view import describe_verdict

    # A verdict the page cannot name must raise, not render blank.
    assert {v.value: describe_verdict(v.value) for v in ReviewVerdict} == {
        "approve": "approved", "modify": "modified", "skipped": "skipped",
    }
    with pytest.raises(HTTPException) as caught:
        describe_verdict("rejected")
    assert "rejected" in str(caught.value.detail)

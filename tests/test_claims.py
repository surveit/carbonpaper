"""A claim is proposed, then stood behind or refused, and its context is ordinary columns."""
from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.models.claims import (
    ClaimImportance,
    ClaimShapeInput,
    ClaimStatus,
    DataUniverseRequirement,
    StageOutputCellCitation,
)
from app.models.records.claims import Claim
from app.models.records.workflow_output import WorkflowOutput
from app.models.schema import Column
from app.services import claim_shapes, claims
from app.services.errors import ClaimRefused

_PROJECT = "ai_lobbying"
_RUN = "20260901T103753.789399"
# Two quarters of filings, and the half-year before them.
_H1 = {"period_start": date(2026, 1, 1), "period_end": date(2026, 6, 30)}
_H2 = {"period_start": date(2025, 7, 1), "period_end": date(2025, 12, 31)}
_TEXT = "US lobbying firms reported $63,027,729 in AI lobbying income in the first half of 2026."
_PERIOD = [
    Column(name="period_start", type="date", nullable=False),
    Column(name="period_end", type="date", nullable=False),
]
_SPEND = ClaimShapeInput(
    label="Reported by outside firms as received for AI lobbying in the United States, in dollars",
    requires=DataUniverseRequirement.closed, importance=ClaimImportance.primary,
    context=_PERIOD,
)
_CLIENTS = ClaimShapeInput(
    label="Clients that paid a US outside firm for AI lobbying",
    requires=DataUniverseRequirement.open, importance=ClaimImportance.secondary,
    context=_PERIOD,
)


def _a_run_of_two_shapes() -> dict[str, str]:
    stored = claim_shapes.write_claim_shapes(_PROJECT, [_SPEND, _CLIENTS])
    ids = {shape.label: shape.id for shape in stored}
    _publish("ai-spend", 63027729.0, ids[_SPEND.label])
    _publish("ai-clients", 723, ids[_CLIENTS.label])
    _publish("corpus-rows", 45061, None)
    return ids


def _publish(slug: str, value, shape_id: str | None, run_id: str = _RUN) -> None:
    WorkflowOutput(
        slug=slug, label=slug, primary=True, shape_id=shape_id,
        citation=StageOutputCellCitation(
            run_id=run_id, stage_id="ai_spend_totals", row_ordinal=0,
            column="ai_spend", value=value,
        ),
    ).save()


def _approve(claim: Claim, run_read_everything: bool = True) -> Claim:
    return claims.approve_claim(_PROJECT, claim.id, run_read_everything)


# ── which outputs can be claimed at all ──────────────────────────────────────
def test_an_output_naming_no_shape_is_not_claimable():
    _a_run_of_two_shapes()

    assert sorted(o.slug for o in claims.read_workflow_run_outputs(_RUN)) == [
        "ai-clients", "ai-spend"
    ]


def test_claiming_an_output_this_run_never_published_is_refused():
    _a_run_of_two_shapes()

    with pytest.raises(ClaimRefused, match="published no claimable output"):
        claims.submit_claim(_PROJECT, _RUN, "corpus-rows", _H1, _TEXT)


# ── the context is the shape's own columns ───────────────────────────────────
def test_a_context_missing_a_column_the_shape_declares_is_refused():
    _a_run_of_two_shapes()

    with pytest.raises(ClaimRefused, match="period_end"):
        claims.submit_claim(_PROJECT, _RUN, "ai-spend", {"period_start": date(2026, 1, 1)}, _TEXT)

    assert Claim.find(created_by_project_id=_PROJECT) == []


def test_a_context_carrying_a_column_the_shape_does_not_declare_is_refused():
    _a_run_of_two_shapes()

    with pytest.raises(ClaimRefused, match="jurisdiction"):
        claims.submit_claim(_PROJECT, _RUN, "ai-spend", {**_H1, "jurisdiction": "US"}, _TEXT)


def test_a_value_of_the_wrong_type_is_refused():
    _a_run_of_two_shapes()

    with pytest.raises(ClaimRefused, match="period_start"):
        claims.submit_claim(_PROJECT, _RUN, "ai-spend", {**_H1, "period_start": "whenever"}, _TEXT)


# ── proposed, then stood behind or refused ───────────────────────────────────
def test_a_submitted_claim_stands_behind_nothing_until_it_is_approved():
    ids = _a_run_of_two_shapes()

    claim = claims.submit_claim(_PROJECT, _RUN, "ai-spend", _H1, _TEXT)

    assert (claim.status, claim.shape_id) == (ClaimStatus.submitted, ids[_SPEND.label])
    assert claim.citation.value == 63027729.0


def test_approving_moves_the_claim_and_writes_no_second_one():
    _a_run_of_two_shapes()
    claim = claims.submit_claim(_PROJECT, _RUN, "ai-spend", _H1, _TEXT)

    approved = _approve(claim)

    assert (approved.id, approved.status) == (claim.id, ClaimStatus.approved)
    assert len(Claim.find(created_by_project_id=_PROJECT)) == 1


def test_a_declined_claim_stays_saying_so_and_is_nobody_s_history():
    ids = _a_run_of_two_shapes()
    claim = claims.submit_claim(_PROJECT, _RUN, "ai-spend", _H1, _TEXT)

    claims.decline_claim(_PROJECT, claim.id)

    assert Claim.load(claim.id).status == ClaimStatus.declined
    assert claims.load_claims_of_shape(_PROJECT, ids[_SPEND.label]) == []


def test_skipping_an_output_leaves_a_declined_claim_rather_than_nothing():
    _a_run_of_two_shapes()

    claims.decline_output(_PROJECT, _RUN, "ai-clients")

    assert [c.status for c in Claim.find(created_by_project_id=_PROJECT)] == ["declined"]


# ── one shape and one context is one fact ────────────────────────────────────
def test_the_same_context_twice_is_refused_as_a_restatement():
    _a_run_of_two_shapes()
    standing = _approve(claims.submit_claim(_PROJECT, _RUN, "ai-spend", _H1, _TEXT))

    with pytest.raises(ClaimRefused, match=f"restates claim {standing.id}"):
        claims.submit_claim(_PROJECT, _RUN, "ai-spend", _H1, _TEXT)


def test_a_different_period_is_a_series_and_goes_through():
    _a_run_of_two_shapes()
    _approve(claims.submit_claim(_PROJECT, _RUN, "ai-spend", _H2, _TEXT))

    claim = claims.submit_claim(_PROJECT, _RUN, "ai-spend", _H1, _TEXT)

    assert claim.context["period_start"] == date(2026, 1, 1).isoformat()


def test_a_declined_claim_no_longer_stands_in_the_way():
    _a_run_of_two_shapes()
    claims.decline_claim(_PROJECT, claims.submit_claim(_PROJECT, _RUN, "ai-spend", _H1, _TEXT).id)

    claim = claims.submit_claim(_PROJECT, _RUN, "ai-spend", _H1, _TEXT)

    assert claim.status == ClaimStatus.submitted


def test_a_claim_is_re_checked_at_approval_because_the_ground_can_move():
    _a_run_of_two_shapes()
    first = claims.submit_claim(_PROJECT, _RUN, "ai-spend", _H1, _TEXT)
    _approve(claims.submit_claim(_PROJECT, _RUN, "ai-clients", _H1, _TEXT))
    second = Claim.load(first.id)
    second.status = ClaimStatus.submitted

    with pytest.raises(ClaimRefused, match="restates claim"):
        _approve(claims.submit_claim(_PROJECT, _RUN, "ai-clients", _H1, _TEXT))
    assert second.id == first.id


# ── a closed shape needs the whole input ─────────────────────────────────────
def test_a_closed_shape_cannot_stand_on_a_run_that_read_a_window():
    _a_run_of_two_shapes()
    claim = claims.submit_claim(_PROJECT, _RUN, "ai-spend", _H1, _TEXT)

    with pytest.raises(ClaimRefused, match="turns 'is' into 'at least'"):
        _approve(claim, run_read_everything=False)


def test_an_open_shape_stands_on_a_windowed_run():
    _a_run_of_two_shapes()
    claim = claims.submit_claim(_PROJECT, _RUN, "ai-clients", _H1, _TEXT)

    assert _approve(claim, run_read_everything=False).status == ClaimStatus.approved


# ── the words are the deliverable ────────────────────────────────────────────
def test_a_claim_without_a_sentence_is_refused():
    _a_run_of_two_shapes()

    with pytest.raises(ClaimRefused, match="write it"):
        claims.submit_claim(_PROJECT, _RUN, "ai-spend", _H1, "   ")


def test_the_words_cannot_move_at_all_because_review_attacks_them():
    _a_run_of_two_shapes()
    claim = claims.submit_claim(_PROJECT, _RUN, "ai-spend", _H1, _TEXT)

    with pytest.raises(ValidationError):
        claim.text = "Something review never saw."


def test_a_better_sentence_can_be_learned_by_the_shape():
    """The template asserts nothing, so it is the one thing on a shape that may move."""
    ids = _a_run_of_two_shapes()

    shape = claims.learn_the_template(
        _PROJECT, ids[_SPEND.label], "Outside firms reported ${value} for AI lobbying."
    )

    assert shape.template == "Outside firms reported ${value} for AI lobbying."

"""What every attacker is handed: the run's outputs, its arms, its columns — and nothing else."""
from __future__ import annotations

import pytest

from app.compiler.claim_attack.evidence import render_evidence_bundle, render_evidence_pool
from app.models.claim_review import (
    Attacker, Challenge, ChallengeKind, Cost, Grounding, Moves, OutputEvidence,
)
from app.models.claims import StageOutputCellCitation
from app.services import claim_review
from app.services.errors import ClaimReviewRefused
from claim_review_fixture import PROJECT, TOTAL_TEXT, claim_the_total, run_the_fixture


@pytest.fixture
def claim(projects_root):
    return claim_the_total(run_the_fixture(projects_root))


def test_the_bundle_holds_the_sentence_the_figures_and_marks_the_cited_one(claim):
    bundle = claim_review.build_evidence_bundle(PROJECT, claim.id)

    assert bundle.claim_text == TOTAL_TEXT
    assert bundle.run_read_everything is True
    by_slug = {o.slug: o for o in bundle.outputs}
    assert by_slug["grant-total"].value == "2200" and by_slug["grant-total"].cited   # render_figure groups from 10,000
    assert by_slug["grant-count"].value == "5" and not by_slug["grant-count"].cited
    assert bundle.shape.universe == "closed"


def test_every_stage_of_the_version_is_there_flagged_by_whether_it_feeds_the_figure(claim):
    bundle = claim_review.build_evidence_bundle(PROJECT, claim.id)

    feeds = {s.stage_id: s.feeds_the_cited_stage for s in bundle.stages}
    assert feeds["grants_only"] and feeds["funded"] and feeds["load_east"]
    assert not feeds["by_portfolio"] and not feeds["grant_totals"]
    assert 'row["kind"] == "grant"' in next(s.code for s in bundle.stages if s.stage_id == "grants_only")


def test_a_sandboxed_filters_predicate_reaches_the_attacker_too(claim):
    bundle = claim_review.build_evidence_bundle(PROJECT, claim.id)

    sandboxed = next(s for s in bundle.stages if s.stage_id == "sandboxed_positive")
    assert 'row["amount"] > 0' in sandboxed.code
    assert 'row["amount"] > 0' in render_evidence_pool(bundle)


def test_an_arm_no_row_took_reads_zero(claim):
    bundle = claim_review.build_evidence_bundle(PROJECT, claim.id)

    kept = next(b for b in bundle.branches
                if b.stage_id == "over_a_million" and b.role == "keeps")
    assert kept.rows_count == 0
    assert "rows 0" in render_evidence_pool(bundle)


def test_a_figure_of_five_digits_is_pooled_with_its_separators():
    cell = StageOutputCellCitation(run_id="r", stage_id="s", row_ordinal=0,
                                   column="ai_spend", value=63027729)

    assert claim_review._read_output_value(cell) == "63,027,729"


def test_the_arms_the_run_recorded_come_with_their_row_counts(claim):
    bundle = claim_review.build_evidence_bundle(PROJECT, claim.id)

    at_funded = [b for b in bundle.branches if b.stage_id == "funded"]
    assert {b.role for b in at_funded} == {"keeps", "removes"}
    assert next(b.rows_count for b in at_funded if b.role == "removes") == 1   # G-007, the zero
    assert not [b for b in bundle.branches if b.reason == "merge"]


def test_each_input_column_is_profiled_off_what_the_run_read(claim):
    bundle = claim_review.build_evidence_bundle(PROJECT, claim.id)

    east_amount = next(c for c in bundle.input_columns
                       if c.stage_id == "load_east" and c.column == "amount")
    assert east_amount.row_count == 6 and east_amount.filled_count == 6
    assert east_amount.kind == "number"
    assert {c.stage_id for c in bundle.input_columns} == {"load_east", "load_west", "load_agencies"}


def test_the_rendering_carries_every_figure_the_guard_will_check_against(claim):
    bundle = claim_review.build_evidence_bundle(PROJECT, claim.id)
    text = render_evidence_bundle(bundle)

    assert TOTAL_TEXT in text and "grant-total" in text and "2200" in text
    assert "----- BRANCHES -----" in text and "----- INPUT COLUMNS -----" in text
    assert "feeds the cited stage: true" in text
    pool = render_evidence_pool(bundle)
    assert TOTAL_TEXT not in pool and "CLAIM:" not in pool and "2200" in pool


def test_a_sentence_carrying_both_quote_marks_stays_verbatim_in_the_pool(projects_root):
    quoted = 'The firm\'s "AI lobbying" income came to 2,200.'
    claim = claim_the_total(run_the_fixture(projects_root), quoted)

    text = render_evidence_bundle(claim_review.build_evidence_bundle(PROJECT, claim.id))

    assert quoted in text


def test_a_table_the_run_published_is_pooled_by_its_row_count(claim):
    from app.models.claims import RowsRectangle, StageOutputTableCitation
    from app.models.records.workflow_output import WorkflowOutput
    WorkflowOutput(
        slug="by-portfolio", label="By portfolio", shape_id=None,
        citation=StageOutputTableCitation(
            run_id=claim.citation.run_id, stage_id="by_portfolio",
            rectangle=RowsRectangle(row_start=0, row_end=3,
                                    columns=["portfolio", "total_amount"]))).save()

    bundle = claim_review.build_evidence_bundle(PROJECT, claim.id)

    table = next(o for o in bundle.outputs if o.slug == "by-portfolio")
    assert table.value == "3 rows" and table.stage_id == "by_portfolio" and not table.cited
    assert "3 rows" in render_evidence_bundle(bundle)


def _challenge(backing: str, grounding_index: int | None = 0) -> Challenge:
    return Challenge(attacker=Attacker.coverage, kind=ChallengeKind.coverage,
                     grounding_index=grounding_index,
                     text="t", evidence="e", backing=backing, severity=2,
                     moves=Moves.moves, cost=Cost.free)


def _grounding() -> list[Grounding]:
    return [Grounding(start=0, end=6, evidence=OutputEvidence(slug="grant-total"),
                      how="the figure")]


def _store(claim, backing: str, corpus: str):
    return claim_review.store_claim_review(
        PROJECT, claim.id, grounding=_grounding(), challenges=[_challenge(backing)],
        rewrites=[], summary="s", session_ids=[], corpus=corpus)


def _pool_of(claim) -> str:
    return render_evidence_pool(claim_review.build_evidence_bundle(PROJECT, claim.id))


def test_the_journalists_own_sentence_cannot_back_a_challenge_against_it(claim):
    with pytest.raises(ClaimReviewRefused, match="2,200"):
        _store(claim, "2,200", _pool_of(claim))
    assert claim_review.load_claim_review(PROJECT, claim.id) is None


def test_the_run_spelling_of_the_same_figure_does_back_it(claim):
    stored = _store(claim, "2200", _pool_of(claim))

    assert claim_review.load_claim_review(PROJECT, claim.id).id == stored.id


def test_a_backing_sitting_inside_a_longer_number_is_not_in_the_pool(claim):
    with pytest.raises(ClaimReviewRefused, match="'220'"):
        _store(claim, "220", "the pool says 2200 in total")


def test_a_standalone_token_however_short_is_backed(claim):
    stored = _store(claim, "5", "grant-count · How many grants · 5 · grant_totals")

    assert stored.challenges[0].backing == "5"


def test_a_challenge_landing_on_a_phrase_the_review_never_grounded_is_refused(claim):
    with pytest.raises(ClaimReviewRefused, match="names no phrase"):
        claim_review.store_claim_review(
            PROJECT, claim.id, grounding=_grounding(),
            challenges=[_challenge("2200", grounding_index=3)],
            rewrites=[], summary="s", session_ids=[], corpus=_pool_of(claim))
    assert claim_review.load_claim_review(PROJECT, claim.id) is None


def test_a_backing_in_neither_the_pool_nor_an_attackers_evidence_is_refused(claim):
    grounding = [Grounding(start=0, end=6, evidence=OutputEvidence(slug="grant-total"), how="the figure")]

    with pytest.raises(ClaimReviewRefused, match="9,999"):
        claim_review.store_claim_review(
            PROJECT, claim.id, grounding=grounding, challenges=[_challenge("9,999 rows")],
            rewrites=[], summary="s", session_ids=[], corpus="the pool says 2,200")
    assert claim_review.load_claim_review(PROJECT, claim.id) is None


def test_a_backing_copied_from_an_attackers_evidence_is_accepted_and_the_review_round_trips(claim):
    grounding = [Grounding(start=0, end=6, evidence=OutputEvidence(slug="grant-total"), how="the figure")]

    stored = claim_review.store_claim_review(
        PROJECT, claim.id, grounding=grounding, challenges=[_challenge("1 of 10 rows")],
        rewrites=[], summary="One row was dropped.", session_ids=["s1"],
        corpus="pool text\nattacker evidence: 1 of 10 rows were zero")

    held = claim_review.load_claim_review(PROJECT, claim.id)
    assert held is not None and held.id == stored.id
    assert held.challenges[0].backing == "1 of 10 rows" and held.session_ids == ["s1"]


def test_a_span_past_the_sentence_or_overlapping_another_is_refused(claim):
    too_far = [Grounding(start=0, end=len(TOTAL_TEXT) + 5, evidence=None, how="x")]
    with pytest.raises(ClaimReviewRefused, match="past the end"):
        claim_review.store_claim_review(PROJECT, claim.id, grounding=too_far, challenges=[],
                                        rewrites=[], summary="s", session_ids=[], corpus="")
    crossing = [Grounding(start=0, end=10, evidence=None, how="x"),
                Grounding(start=5, end=12, evidence=None, how="y")]
    with pytest.raises(ClaimReviewRefused, match="overlap"):
        claim_review.store_claim_review(PROJECT, claim.id, grounding=crossing, challenges=[],
                                        rewrites=[], summary="s", session_ids=[], corpus="")


def test_a_table_claim_is_refused_with_the_reason(projects_root):
    from app.models.claims import RowsRectangle, StageOutputTableCitation
    from app.models.records.workflow_output import WorkflowOutput
    from app.services import claim_shapes, claims
    from claim_review_fixture import TOTAL_SHAPE
    run_id = run_the_fixture(projects_root)
    [shape] = claim_shapes.write_claim_shapes(PROJECT, [TOTAL_SHAPE])
    WorkflowOutput(slug="by-portfolio", label="By portfolio", shape_id=shape.id,
                   citation=StageOutputTableCitation(
                       run_id=run_id, stage_id="by_portfolio",
                       rectangle=RowsRectangle(row_start=0, row_end=3, columns=["portfolio", "total_amount"]))).save()
    table_claim = claims.submit_claim(PROJECT, run_id, "by-portfolio", {}, "Health took the most.")

    with pytest.raises(ClaimReviewRefused, match="table claim"):
        claim_review.build_evidence_bundle(PROJECT, table_claim.id)
    with pytest.raises(ClaimReviewRefused, match="table claim"):
        claim_review.store_claim_review(PROJECT, table_claim.id, grounding=[], challenges=[],
                                        rewrites=[], summary="s", session_ids=[], corpus="")

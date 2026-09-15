"""What every attacker is handed, and what a review must hold before it is stored."""
from __future__ import annotations

import pytest

from app.reviewer.evidence import render_evidence_bundle, render_evidence_pool
from app.models.citations import (
    StageCitation,
    StageOutputCellCitation,
    StageOutputColumnCitation,
    TermCitation,
)
from app.models.named_schemas import NamedSchema, SchemaLibrary
from app.models.records.claim_review import Challenge, ChallengeKind, ClaimPart
from app.models.terms import Terms, Verb
from app.services import claim_review
from app.services import terms as terms_service
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
    from app.models.citations import RowsRectangle, StageOutputTableCitation
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


def _challenge(evidence: str, **overrides: object) -> Challenge:
    fields = dict(kind=ChallengeKind.coverage, claim_part_index=0, text="t",
                  justification="j", evidence=evidence, severity=2)
    return Challenge.model_validate({**fields, **overrides})


_PARTS = [ClaimPart(phrase="Grants"), ClaimPart(phrase="2,200")]
_SESSIONS = ["session-parts", "session-data", "session-orchestrator"]
_NO_CHALLENGES: list[Challenge] = []


def _store(claim, *, claim_parts: list[ClaimPart] = _PARTS,
           challenges: list[Challenge] = _NO_CHALLENGES, summary: str = "s", corpus: str = ""):
    return claim_review.store_claim_review(
        PROJECT, claim.id, claim_parts=claim_parts, challenges=challenges, summary=summary,
        session_ids=_SESSIONS, corpus=corpus)


def _store_evidence(claim, evidence: str, corpus: str):
    return _store(claim, challenges=[_challenge(evidence)], corpus=corpus)


def _pool_of(claim) -> str:
    return render_evidence_pool(claim_review.build_evidence_bundle(PROJECT, claim.id))


def _refusals_of(claim, *, claim_parts: list[ClaimPart] = _PARTS,
                 challenges: list[Challenge] = _NO_CHALLENGES, corpus: str = "") -> list[str]:
    with pytest.raises(ClaimReviewRefused) as refused:
        _store(claim, claim_parts=claim_parts, challenges=challenges, corpus=corpus)
    assert claim_review.load_claim_review(claim.id) is None
    return refused.value.refusals


# ── evidence the pool prints ──


def test_the_claim_owners_own_sentence_cannot_be_evidence_against_it(claim):
    with pytest.raises(ClaimReviewRefused, match="evidence '2,200' is on no line of the pool"):
        _store_evidence(claim, "2,200", _pool_of(claim))
    assert claim_review.load_claim_review(claim.id) is None


def test_the_run_spelling_of_the_same_figure_is_evidence(claim):
    stored = _store_evidence(claim, "2200", _pool_of(claim))

    assert claim_review.load_claim_review(claim.id).id == stored.id


def test_evidence_sitting_inside_a_longer_number_is_not_in_the_pool(claim):
    with pytest.raises(ClaimReviewRefused, match="'220'"):
        _store_evidence(claim, "220", "the pool says 2200 in total")


def test_a_standalone_token_however_short_is_evidence(claim):
    stored = _store_evidence(claim, "5", "grant-count · How many grants · 5 · grant_totals")

    assert stored.challenges[0].evidence == "5"


def test_evidence_ending_at_a_full_stop_in_the_pool_is_printed(claim):
    stored = _store_evidence(claim, "28% of records", "the pool says 28% of records. And more.")

    assert stored.challenges[0].evidence == "28% of records"


def test_a_digit_cut_out_of_a_thousands_separated_number_is_not_in_the_pool(claim):
    with pytest.raises(ClaimReviewRefused, match="evidence '2' is"):
        _store_evidence(claim, "2", "the pool says 2,200 in total")


def test_empty_evidence_is_refused(claim):
    # A colon then a space is a token boundary, where an empty pattern would match.
    assert _refusals_of(claim, challenges=[_challenge("")], corpus="the pool says: 2200") == [
        "challenge 0 (coverage): evidence '' is on no line of the pool"]


def test_evidence_copied_from_an_attackers_text_is_accepted_and_the_review_round_trips(claim):
    stored = _store(claim, challenges=[_challenge("1 of 10 rows")], summary="One row was dropped.",
                    corpus="pool text\nattacker evidence: 1 of 10 rows were zero")

    held = claim_review.load_claim_review(claim.id)
    assert held is not None and held.id == stored.id
    assert held.challenges[0].evidence == "1 of 10 rows" and held.claim_parts == _PARTS
    assert held.session_ids == _SESSIONS


# ── claim parts ──


def test_a_phrase_the_claim_holds_once_asked_for_a_second_time_is_refused(claim):
    assert _refusals_of(claim, claim_parts=[ClaimPart(phrase="Grants", occurrence=2)]) == [
        "claim part 0 'Grants': the claim holds it fewer than 2 times"]


def test_a_phrase_the_claim_never_holds_is_refused(claim):
    assert _refusals_of(claim, claim_parts=[ClaimPart(phrase="grants")]) == [
        "claim part 0 'grants': the claim holds it fewer than 1 times"]


def test_claim_parts_that_overlap_are_refused_naming_both(claim):
    overlapping = [ClaimPart(phrase="Grants came"), ClaimPart(phrase="2,200"),
                   ClaimPart(phrase="came to")]

    assert _refusals_of(claim, claim_parts=overlapping) == ["claim parts 0 and 2 overlap"]


def test_claim_parts_that_only_touch_are_accepted(claim):
    stored = _store(claim, claim_parts=[ClaimPart(phrase="Grants"), ClaimPart(phrase=" came to")])

    assert [part.phrase for part in stored.claim_parts] == ["Grants", " came to"]


# ── challenges ──


def test_a_challenge_landing_on_a_claim_part_the_review_does_not_hold_is_refused(claim):
    challenges = [_challenge("2200", claim_part_index=2)]

    assert _refusals_of(claim, challenges=challenges, corpus=_pool_of(claim)) == [
        "challenge 0 (coverage): claim_part_index 2 names no claim part; the claim has 2"]


def test_a_challenge_on_the_last_claim_part_or_the_whole_sentence_is_accepted(claim):
    challenges = [_challenge("2200", claim_part_index=1), _challenge("2200", claim_part_index=None)]

    assert len(_store(claim, challenges=challenges, corpus=_pool_of(claim)).challenges) == 2



# ── citations ──


def _cell(claim, **overrides: object) -> StageOutputCellCitation:
    fields = dict(run_id=claim.citation.run_id, stage_id="grant_totals", row_ordinal=0,
                  column="total_amount", value=2200)
    return StageOutputCellCitation.model_validate({**fields, **overrides})


def _store_citing(claim, citations: list):
    return _store(claim, challenges=[_challenge("2200", citations=citations)],
                  corpus=_pool_of(claim))


def _refuse_citing(claim, citation) -> str:
    [refusal] = _refusals_of(claim, challenges=[_challenge("2200", citations=[citation])],
                             corpus=_pool_of(claim))
    assert refusal.startswith(f"challenge 0 (coverage): {citation.kind} citation ")
    return refusal


def test_a_cell_the_run_holds_is_accepted_as_a_number_or_as_its_text(claim):
    stored = _store_citing(claim, [_cell(claim), _cell(claim, value="2200"),
                                   _cell(claim, column="grants", value=5)])

    assert len(stored.challenges[0].citations) == 3


@pytest.mark.parametrize("overrides, fragment", [
    ({"run_id": "another_run"}, "names run 'another_run', not the claim's run"),
    ({"stage_id": "no_such_stage"}, "names stage 'no_such_stage', which wrote no output"),
    ({"row_ordinal": 1}, "names row 1, which the output of 'grant_totals' does not hold"),
    ({"column": "no_such_column"}, "names column 'no_such_column', which the output of"),
    ({"value": 2201}, "gives value 2201, but that cell holds '2200'"),
    ({"value": "2,200"}, "gives value '2,200', but that cell holds '2200'"),
])
def test_a_fabricated_cell_is_refused(claim, overrides, fragment):
    assert fragment in _refuse_citing(claim, _cell(claim, **overrides))


def test_a_column_the_stage_output_holds_is_accepted(claim):
    column = StageOutputColumnCitation(stage_id="grant_totals", column="grants")

    assert _store_citing(claim, [column]).challenges[0].citations == [column]


@pytest.mark.parametrize("stage_id, column, fragment", [
    ("grant_totals", "no_such_column", "names column 'no_such_column', which the output of"),
    ("no_such_stage", "grants", "names stage 'no_such_stage', which wrote no output"),
])
def test_a_fabricated_column_is_refused(claim, stage_id, column, fragment):
    citation = StageOutputColumnCitation(stage_id=stage_id, column=column)

    assert fragment in _refuse_citing(claim, citation)


def test_a_stage_of_the_runs_workflow_is_accepted_and_one_it_lacks_is_refused(claim):
    assert "the run's workflow does not hold" in _refuse_citing(
        claim, StageCitation(stage_id="no_such_stage"))
    assert _store_citing(claim, [StageCitation(stage_id="funded")]).challenges[0].citations


def _write_a_noun_and_a_verb() -> None:
    terms_service.write_terms(PROJECT, Terms(
        nouns=SchemaLibrary(schemas=[NamedSchema(name="grant", title="A grant")]),
        verbs=[Verb(name="funded", definition="Paid out.")]))


def test_a_term_the_project_defines_is_accepted_and_one_it_lacks_is_refused(claim):
    _write_a_noun_and_a_verb()

    assert "no noun or verb in the project's terms" in _refuse_citing(
        claim, TermCitation(name="grants"))
    stored = _store_citing(claim, [TermCitation(name="grant"), TermCitation(name="funded")])
    assert len(stored.challenges[0].citations) == 2


def test_a_table_claim_is_refused_with_the_reason(projects_root):
    from app.models.citations import RowsRectangle, StageOutputTableCitation
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
        _store(table_claim, claim_parts=[])

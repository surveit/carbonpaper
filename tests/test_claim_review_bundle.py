"""What every reviewer is handed, and what a review must hold before it is stored."""
from __future__ import annotations

import re

import pytest

from app.core.file_shape import ColumnShape
from app.reviewer.evidence import render_evidence_bundle, render_evidence_pool
from app.models.citations import (
    StageCitation,
    StageOutputCellCitation,
    StageOutputColumnCitation,
    TermCitation,
    render_citation_value,
)
from app.models.claims import ClaimShapeInput
from app.models.named_schemas import NamedSchema, SchemaLibrary
from app.models.records.claim_review import Challenge, ChallengeKind, ClaimPart
from app.models.terms import Terms, Verb
from app.services import claim_review
from app.services import terms as terms_service
from app.services.errors import ClaimReviewRefused
from claim_review_fixture import (
    PROJECT,
    TOTAL_SHAPE,
    TOTAL_TEXT,
    claim_the_total,
    run_the_fixture,
)


@pytest.fixture
def claim(projects_root):
    return claim_the_total(run_the_fixture(projects_root))


def test_the_bundle_holds_the_sentence_the_outputs_with_their_citations_and_the_shape(claim):
    bundle = claim_review.build_evidence_bundle(PROJECT, claim.id)

    assert bundle.claim_text == TOTAL_TEXT
    assert bundle.run_read_everything is True
    by_slug = {o.slug: o for o in bundle.outputs}
    assert by_slug["grant-total"].citation == claim.citation and bundle.cited_slug == "grant-total"
    assert render_citation_value(by_slug["grant-count"].citation) == "5"
    assert type(bundle.shape) is ClaimShapeInput and bundle.shape == TOTAL_SHAPE


_OUTPUT_LINE = re.compile(
    r"^(?P<slug>\S+) · .+ · (?P<value>[^·]+) · stage `(?P<stage_id>[^`]+)`, "
    r"row (?P<row_ordinal>\d+), column `(?P<column>[^`]+)`")


def _find_output_lines(pool: str) -> dict[str, re.Match[str]]:
    return {match["slug"]: match for line in pool.splitlines()
            if (match := _OUTPUT_LINE.match(line)) is not None}


def test_the_pool_opens_with_the_run_and_says_where_each_cell_output_sits(claim):
    pool = render_evidence_pool(claim_review.build_evidence_bundle(PROJECT, claim.id))

    assert pool.splitlines()[0] == f"run: {claim.citation.run_id}"
    lines = _find_output_lines(pool)
    assert lines["grant-total"].group(0).endswith("column `total_amount`")
    assert lines["grant-count"].group(0).endswith("column `grants`")
    assert "CITED" in lines["grant-total"].string and "CITED" not in lines["grant-count"].string


@pytest.mark.parametrize("slug", ["grant-total", "grant-count"])
def test_a_cell_citation_copied_off_the_pool_is_one_the_store_accepts(claim, slug):
    pool = _pool_of(claim)
    printed = _find_output_lines(pool)[slug]
    copied = StageOutputCellCitation(
        run_id=pool.splitlines()[0].removeprefix("run: "), stage_id=printed["stage_id"],
        row_ordinal=int(printed["row_ordinal"]), column=printed["column"],
        value=printed["value"])

    challenge = _challenge(printed["value"], citations=[copied])
    assert claim_review.find_citation_issues(PROJECT, claim.citation.run_id, [challenge]) == []


def test_a_cell_output_of_five_digits_is_pooled_as_its_figure(claim):
    from app.models.records.workflow_output import WorkflowOutput
    WorkflowOutput(
        slug="ai-spend", label="AI spend", shape_id=None,
        citation=StageOutputCellCitation(run_id=claim.citation.run_id, stage_id="grant_totals",
                                         row_ordinal=0, column="total_amount",
                                         value=63027729)).save()

    lines = _find_output_lines(_pool_of(claim))

    assert lines["ai-spend"]["value"] == "63,027,729"


def test_every_stage_of_the_version_is_there_flagged_by_whether_it_feeds_the_figure(claim):
    bundle = claim_review.build_evidence_bundle(PROJECT, claim.id)

    feeds = {s.stage_id: s.feeds_the_cited_stage for s in bundle.stages}
    assert feeds["grants_only"] and feeds["funded"] and feeds["load_east"]
    assert not feeds["by_portfolio"] and not feeds["grant_totals"]
    assert 'row["kind"] == "grant"' in next(s.code for s in bundle.stages if s.stage_id == "grants_only")


def test_a_sandboxed_filters_predicate_reaches_the_reviewer_too(claim):
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


def test_the_arms_the_run_recorded_come_with_their_row_counts(claim):
    bundle = claim_review.build_evidence_bundle(PROJECT, claim.id)

    at_funded = [b for b in bundle.branches if b.stage_id == "funded"]
    assert {b.role for b in at_funded} == {"keeps", "removes"}
    assert next(b.rows_count for b in at_funded if b.role == "removes") == 1   # G-007, the zero
    assert not [b for b in bundle.branches if b.reason == "merge"]


def test_each_input_column_is_profiled_off_what_the_run_read(claim):
    bundle = claim_review.build_evidence_bundle(PROJECT, claim.id)

    east_amount = next(c for c in bundle.input_columns
                       if c.stage_id == "load_east" and c.shape.column == "amount")
    assert type(east_amount.shape) is ColumnShape
    assert east_amount.row_count == 6 and east_amount.shape.filled_count == 6
    assert east_amount.shape.kind == "number"
    assert {c.stage_id for c in bundle.input_columns} == {"load_east", "load_west", "load_agencies"}


def test_the_rendering_carries_every_figure_the_guard_will_check_against(claim):
    bundle = claim_review.build_evidence_bundle(PROJECT, claim.id)
    text = render_evidence_bundle(bundle)

    assert TOTAL_TEXT in text and "grant-total" in text and "2200" in text
    assert "----- BRANCHES -----" in text and "----- INPUT COLUMNS -----" in text
    assert "feeds the cited stage: true" in text
    assert "universe: closed · importance: primary" in text
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
    assert render_citation_value(table.citation) == "3 rows"
    assert table.citation.stage_id == "by_portfolio" and bundle.cited_slug != "by-portfolio"
    assert "by-portfolio · By portfolio · 3 rows · stage `by_portfolio`" in render_evidence_pool(bundle)


def _challenge(evidence: str, **overrides: object) -> Challenge:
    fields = dict(kind=ChallengeKind.coverage, claim_part_index=0, text="t",
                  justification="j", evidence=evidence, severity=2)
    return Challenge.model_validate({**fields, **overrides})


_PARTS = [ClaimPart(phrase="Grants"), ClaimPart(phrase="2,200")]
_SESSIONS = ["session-parts", "session-data", "session-orchestrator"]
_NO_CHALLENGES: list[Challenge] = []


def _store(claim, *, claim_parts: list[ClaimPart] = _PARTS,
           challenges: list[Challenge] = _NO_CHALLENGES, summary: str = "s"):
    return claim_review.store_claim_review(
        PROJECT, claim.id, claim_parts=claim_parts, challenges=challenges, summary=summary,
        session_ids=_SESSIONS)


def _store_evidence(claim, evidence: str):
    return _store(claim, challenges=[_challenge(evidence)])


def _pool_of(claim) -> str:
    return render_evidence_pool(claim_review.build_evidence_bundle(PROJECT, claim.id))


def _refusals_of(claim, *, claim_parts: list[ClaimPart] = _PARTS,
                 challenges: list[Challenge] = _NO_CHALLENGES) -> list[str]:
    with pytest.raises(ClaimReviewRefused) as refused:
        _store(claim, claim_parts=claim_parts, challenges=challenges)
    assert claim_review.load_claim_review(claim.id) is None
    return refused.value.refusals


# ── evidence the pool prints ──


def test_the_claim_owners_own_sentence_cannot_be_evidence_against_it(claim):
    with pytest.raises(ClaimReviewRefused, match="evidence '2,200' is on no line of the pool"):
        _store_evidence(claim, "2,200")
    assert claim_review.load_claim_review(claim.id) is None


def test_the_run_spelling_of_the_same_figure_is_evidence(claim):
    stored = _store_evidence(claim, "2200")

    assert claim_review.load_claim_review(claim.id).id == stored.id


def test_evidence_sitting_inside_a_longer_number_is_not_in_the_pool(claim):
    assert " 2200" in _pool_of(claim)
    with pytest.raises(ClaimReviewRefused, match="'220'"):
        _store_evidence(claim, "220")


def test_a_standalone_token_however_short_is_evidence(claim):
    assert " · 5 · " in _pool_of(claim)
    stored = _store_evidence(claim, "5")

    assert stored.challenges[0].evidence == "5"


def test_evidence_ending_at_a_full_stop_in_the_pool_is_printed(claim):
    assert "Drops the grants recorded at zero." in _pool_of(claim)
    stored = _store_evidence(claim, "Drops the grants recorded at zero")

    assert stored.challenges[0].evidence == "Drops the grants recorded at zero"


def test_a_digit_cut_out_of_a_thousands_separated_number_is_not_in_the_pool(claim):
    from app.models.records.workflow_output import WorkflowOutput
    WorkflowOutput(
        slug="ai-spend", label="AI spend", shape_id=None,
        citation=StageOutputCellCitation(run_id=claim.citation.run_id, stage_id="grant_totals",
                                         row_ordinal=0, column="total_amount",
                                         value=63027729)).save()
    assert " 63,027,729 " in _pool_of(claim)

    with pytest.raises(ClaimReviewRefused, match="evidence '63' is"):
        _store_evidence(claim, "63")


def test_empty_evidence_is_refused(claim):
    # A colon then a space is a token boundary, where an empty pattern would match.
    assert ": " in _pool_of(claim)
    assert _refusals_of(claim, challenges=[_challenge("")]) == [
        "challenge 0 (coverage): evidence '' is on no line of the pool"]


def test_evidence_found_only_outside_the_pool_is_refused(claim):
    assert "1 of 10 rows" not in _pool_of(claim)

    assert _refusals_of(claim, challenges=[_challenge("1 of 10 rows")]) == [
        "challenge 0 (coverage): evidence '1 of 10 rows' is on no line of the pool"]


def test_evidence_copied_off_the_pool_is_accepted_and_the_review_round_trips(claim):
    copied = "Keeps the grants, dropping the loans"
    assert copied in _pool_of(claim)

    stored = _store(claim, challenges=[_challenge(copied)], summary="One row was dropped.")

    held = claim_review.load_claim_review(claim.id)
    assert held is not None and held.id == stored.id
    assert held.challenges[0].evidence == copied and held.claim_parts == _PARTS
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

    assert _refusals_of(claim, challenges=challenges) == [
        "challenge 0 (coverage): claim_part_index 2 names no claim part; the claim has 2"]


def test_a_challenge_on_the_last_claim_part_or_the_whole_sentence_is_accepted(claim):
    challenges = [_challenge("2200", claim_part_index=1), _challenge("2200", claim_part_index=None)]

    assert len(_store(claim, challenges=challenges).challenges) == 2



# ── citations ──


def _cell(claim, **overrides: object) -> StageOutputCellCitation:
    fields = dict(run_id=claim.citation.run_id, stage_id="grant_totals", row_ordinal=0,
                  column="total_amount", value=2200)
    return StageOutputCellCitation.model_validate({**fields, **overrides})


def _store_citing(claim, citations: list):
    return _store(claim, challenges=[_challenge("2200", citations=citations)])


def _refuse_citing(claim, citation) -> str:
    [refusal] = _refusals_of(claim, challenges=[_challenge("2200", citations=[citation])])
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
    # The output's last row holds 2200, so a negative index would otherwise find it.
    ({"row_ordinal": -1}, "names row -1, which the output of 'grant_totals' does not hold"),
    ({"column": "no_such_column"}, "names column 'no_such_column', which the output of"),
    ({"value": 2201}, "gives value 2201, but that cell holds '2200'"),
    ({"value": "2,200"}, "gives value '2,200', but that cell holds '2200'"),
])
def test_a_fabricated_cell_is_refused(claim, overrides, fragment):
    assert fragment in _refuse_citing(claim, _cell(claim, **overrides))


@pytest.mark.parametrize("value, problem", [
    ("22,000", None),
    ("22000", "gives value '22000', but that cell holds '22,000'"),
])
def test_a_cell_of_five_digits_is_cited_with_its_separators(value, problem):
    output = claim_review._StageOutput(
        row_count=1, columns=frozenset({"total"}), cells_by_column={"total": [22000]})
    held = claim_review._RunHoldings(
        run_id="r", outputs_by_stage_id={"s": output}, stage_ids={"s"}, term_names=set())
    cited = StageOutputCellCitation(run_id="r", stage_id="s", row_ordinal=0,
                                    column="total", value=value)

    assert claim_review._find_cell_problem(held, cited) == problem


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

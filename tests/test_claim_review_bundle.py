"""What every reviewer is handed, and what a review must hold before it is stored."""
from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from app.core.file_shape import ColumnShape
from app.reviewer.evidence import render_evidence_bundle, render_evidence_pool
from app.models.citations import (
    address_citation,
    StageCitation,
    StageOutputCellCitation,
    StageOutputColumnCitation,
    TermCitation,
    render_citation_value,
)
from app.models.claims import ClaimShapeInput
from app.models.named_schemas import NamedSchema, SchemaLibrary
from app.models.records.claim_review import (
    ChallengeKind,
    ClaimPart,
    DraftChallenge,
    Severity,
)
from app.models.terms import Terms, Verb
from app.services import claim_review
from app.services import terms as terms_service
from app.services.errors import ClaimReviewRefused
from claim_review_fixture import (
    NULLABLE_PROJECT,
    PROJECT,
    TOTAL_SHAPE,
    TOTAL_TEXT,
    claim_a_nullable_figure,
    claim_the_total,
    run_the_fixture,
)


@pytest.fixture
def claim(projects_root):
    return claim_the_total(run_the_fixture(projects_root))


@pytest.fixture
def nullable_claim(projects_root):
    return claim_a_nullable_figure(projects_root)


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


@pytest.mark.parametrize("claim_fixture, project_id, slug", [
    ("claim", PROJECT, "grant-total"),
    ("claim", PROJECT, "grant-count"),
    # An int column holding a null: 22,000 is grouped in the pool, 2200 is not.
    ("nullable_claim", NULLABLE_PROJECT, "doubled-large"),
    ("nullable_claim", NULLABLE_PROJECT, "doubled-small"),
])
def test_a_cell_citation_copied_off_the_pool_is_one_the_store_accepts(
        request, claim_fixture, project_id, slug):
    held = request.getfixturevalue(claim_fixture)
    pool = render_evidence_pool(claim_review.build_evidence_bundle(project_id, held.id))
    printed = _find_output_lines(pool)[slug]
    copied = StageOutputCellCitation(
        run_id=pool.splitlines()[0].removeprefix("run: "),
        stage_id=printed["stage_id"], row_ordinal=int(printed["row_ordinal"]),
        column=printed["column"], value=printed["value"])

    stored = claim_review.store_claim_review(
        project_id, held.id, summary="s", session_ids=_SESSIONS,
        challenges=[_challenge(held, claim_part=None, citations=[copied])])

    assert stored.challenges[0].citations == [address_citation(project_id, copied)]


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


def _cell(claim, **overrides: object) -> StageOutputCellCitation:
    fields = dict(run_id=claim.citation.run_id, stage_id="grant_totals", row_ordinal=0,
                  column="total_amount", value=2200)
    return StageOutputCellCitation.model_validate({**fields, **overrides})


def _challenge(claim, **overrides: object) -> DraftChallenge:
    fields = dict(kind=ChallengeKind.coverage, claim_part=ClaimPart(phrase="Grants"), text="t",
                  justification="j", citations=[_cell(claim)], severity=Severity.major)
    return DraftChallenge.model_validate({**fields, **overrides})


_SESSIONS = ["session-parts", "session-data", "session-orchestrator"]
_NO_CHALLENGES: list[DraftChallenge] = []


def _pool_of(claim) -> str:
    return render_evidence_pool(claim_review.build_evidence_bundle(PROJECT, claim.id))


def _store(claim, *, challenges: list[DraftChallenge] = _NO_CHALLENGES, summary: str = "s"):
    return claim_review.store_claim_review(
        PROJECT, claim.id, challenges=challenges, summary=summary, session_ids=_SESSIONS)


def _refusals_of(claim, *, challenges: list[DraftChallenge] = _NO_CHALLENGES) -> list[str]:
    with pytest.raises(ClaimReviewRefused) as refused:
        _store(claim, challenges=challenges)
    assert claim_review.load_claim_review(claim.id) is None
    return refused.value.refusals


# ── every challenge but a gap points into the run ──


def test_a_challenge_citing_nothing_is_refused(claim):
    with pytest.raises(ValidationError, match="a coverage challenge cites nothing in the run"):
        _challenge(claim, citations=[])


def test_a_gap_challenge_needs_no_citation(claim):
    stored = _store(claim, challenges=[
        DraftChallenge(kind=ChallengeKind.gap, claim_part=ClaimPart(phrase="Grants"), text="t",
                  justification="j", severity=Severity.misleading)])

    assert stored.challenges[0].citations == []


# ── the phrase a challenge lands on ──


def test_a_phrase_the_claim_holds_once_asked_for_a_second_time_is_refused(claim):
    landed = _challenge(claim, claim_part=ClaimPart(phrase="Grants", occurrence=2))

    assert _refusals_of(claim, challenges=[landed]) == [
        "challenge 0 (coverage): the claim holds 'Grants' fewer than 2 times"]


def test_a_phrase_the_claim_never_holds_is_refused(claim):
    landed = _challenge(claim, claim_part=ClaimPart(phrase="grants"))

    assert _refusals_of(claim, challenges=[landed]) == [
        "challenge 0 (coverage): the claim holds 'grants' fewer than 1 times"]


def test_two_challenges_may_land_on_overlapping_phrases(claim):
    overlapping = [_challenge(claim, claim_part=ClaimPart(phrase="Grants came")),
                   _challenge(claim, claim_part=ClaimPart(phrase="came to"))]

    assert len(_store(claim, challenges=overlapping).challenges) == 2


def test_a_challenge_about_the_whole_sentence_lands_on_no_phrase(claim):
    stored = _store(claim, challenges=[_challenge(claim, claim_part=None)])

    assert stored.challenges[0].claim_part is None


# ── citations ──


def _store_citing(claim, citations: list):
    return _store(claim, challenges=[_challenge(claim, citations=citations)])


def _refuse_citing(claim, citation) -> str:
    [refusal] = _refusals_of(claim, challenges=[_challenge(claim, citations=[citation])])
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


@pytest.mark.parametrize("value, problems", [
    ("22,000", []),
    ("22000", ["challenge 0 (coverage): stage_output_cell citation gives value '22000', "
               "but that cell holds '22,000'"]),
])
def test_a_cell_from_ten_thousand_is_cited_as_the_pool_groups_it(nullable_claim, value, problems):
    run_id = nullable_claim.citation.run_id
    cited = StageOutputCellCitation(run_id=run_id,
                                    stage_id="doubled", row_ordinal=0, column="doubled",
                                    value=value)

    assert claim_review.find_citation_issues(
        NULLABLE_PROJECT, run_id, [_challenge(nullable_claim, citations=[cited])]) == problems


def test_a_column_the_stage_output_holds_is_accepted(claim):
    column = StageOutputColumnCitation(run_id=claim.citation.run_id, stage_id="grant_totals", column="grants")

    stored = _store_citing(claim, [column]).challenges[0]

    assert stored.citations == [address_citation(PROJECT, column)]


@pytest.mark.parametrize("stage_id, column, fragment", [
    ("grant_totals", "no_such_column", "names column 'no_such_column', which the output of"),
    ("no_such_stage", "grants", "names stage 'no_such_stage', which wrote no output"),
])
def test_a_fabricated_column_is_refused(claim, stage_id, column, fragment):
    citation = StageOutputColumnCitation(run_id=claim.citation.run_id, stage_id=stage_id, column=column)

    assert fragment in _refuse_citing(claim, citation)


def test_a_stage_of_the_runs_workflow_is_accepted_and_one_it_lacks_is_refused(claim):
    assert "the run's workflow does not hold" in _refuse_citing(
        claim, StageCitation(stage_id="no_such_stage"))
    assert _store_citing(claim, [StageCitation(stage_id="funded")]).challenges[0].citations


def _write_a_table_and_a_verb() -> None:
    terms_service.write_terms(PROJECT, Terms(
        schemas=SchemaLibrary(schemas=[NamedSchema(name="grant", title="A grant")]),
        verbs=[Verb(name="funded", definition="Paid out.")]))


def test_a_term_the_project_defines_is_accepted_and_one_it_lacks_is_refused(claim):
    _write_a_table_and_a_verb()

    assert "which the project's terms do not define" in _refuse_citing(
        claim, TermCitation(name="grants"))
    stored = _store_citing(claim, [TermCitation(name="grant"), TermCitation(name="funded")])
    assert len(stored.challenges[0].citations) == 2


def test_a_table_claim_is_refused_with_the_reason(projects_root):
    from app.models.citations import RowsRectangle, StageOutputTableCitation
    from app.models.records.workflow_output import WorkflowOutput
    from app.services import claim_shapes, claims
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
        _store(table_claim)

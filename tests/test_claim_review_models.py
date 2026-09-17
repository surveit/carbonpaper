"""The review's models: claim parts, severities and citation kinds are shaped before any agent runs."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.citations import StageCitation, StageOutputCellCitation
from app.models.claim_review import (
    ChallengesAnswer,
    find_claim_part_spans,
)
from app.models.records.claim_review import (
    Severity,
    SEVERITY_WORDS,
    DraftChallenge,
    ChallengeKind,
    ClaimPart,
    ClaimReview,
)


def _challenge(**overrides: object) -> DraftChallenge:
    fields = dict(kind=ChallengeKind.coverage, claim_part=ClaimPart(phrase="Blank outcomes"),
                  text="Blank outcomes count as unknown.",
                  justification="28% of records are blank.",
                  citations=[StageCitation(stage_id="outcomes")], severity=3)
    return DraftChallenge.model_validate({**fields, **overrides})


def test_a_claim_part_is_found_at_the_occurrence_it_names():
    text = "grants rose; grants fell"
    parts = [ClaimPart(phrase="grants"), ClaimPart(phrase="grants", occurrence=2)]

    assert find_claim_part_spans(text, parts) == [(0, 6), (13, 19)]


def test_overlapping_repeats_of_a_phrase_each_count_as_an_occurrence():
    assert find_claim_part_spans("a a a", [ClaimPart(phrase="a a", occurrence=2)]) == [(2, 5)]


def test_a_phrase_the_claim_holds_fewer_times_than_asked_has_no_span():
    parts = [ClaimPart(phrase="grants", occurrence=2), ClaimPart(phrase="fell")]

    assert find_claim_part_spans("grants rose", parts) == [None, None]


def test_a_claim_part_needs_a_phrase_and_counts_occurrences_from_one():
    with pytest.raises(ValidationError):
        ClaimPart(phrase="")
    with pytest.raises(ValidationError):
        ClaimPart(phrase="grants", occurrence=0)


def test_a_challenge_carries_exactly_these_fields_in_this_order():
    assert list(DraftChallenge.model_fields) == [
        "kind", "claim_part", "text", "justification", "citations", "severity"]


def test_severity_runs_from_zero_to_three_and_each_has_a_word():
    assert [_challenge(severity=s).severity for s in range(4)] == [0, 1, 2, 3]
    assert set(SEVERITY_WORDS) == set(Severity)
    with pytest.raises(ValidationError):
        _challenge(severity=4)
    with pytest.raises(ValidationError):
        _challenge(severity=-1)
    spelled = DraftChallenge.model_fields["severity"].description or ""
    assert spelled.startswith("How wrong a reader is left. ")
    assert all(f"{level} {word}" in spelled for level, word in SEVERITY_WORDS.items())


def test_the_two_highest_severities_divide_on_quantity_and_quality():
    assert "the figure moves materially" in SEVERITY_WORDS[Severity.major]
    assert SEVERITY_WORDS[Severity.misleading].startswith("a reader draws a conclusion")


def test_a_review_holds_no_claim_parts_of_its_own():
    assert "claim_parts" not in ClaimReview.model_fields


def test_a_citation_is_told_apart_by_its_kind():
    parsed = _challenge(citations=[
        {"kind": "stage_output_cell", "run_id": "r", "stage_id": "s",
         "row_ordinal": 0, "column": "c", "value": 1},
        {"kind": "stage_output_column", "run_id": "r", "stage_id": "s", "column": "c"},
        {"kind": "stage", "stage_id": "s"},
        {"kind": "term", "name": "grant"},
    ])

    assert [citation.kind for citation in parsed.citations] == [
        "stage_output_cell", "stage_output_column", "stage", "term"]
    with pytest.raises(ValidationError) as exc:
        _challenge(citations=[{"kind": "branch", "branch_id": "b"}])
    assert exc.value.errors()[0]["type"] == "union_tag_invalid"


def test_every_field_an_agent_fills_on_a_cell_citation_is_described():
    described = {name: field.description for name, field in
                 StageOutputCellCitation.model_fields.items() if name != "kind"}

    assert set(described) == {"run_id", "stage_id", "row_ordinal", "column", "value"}
    assert all(described.values())


def test_no_answer_carries_a_suggested_rewrite():
    with pytest.raises(ValidationError):
        ChallengesAnswer.model_validate({"challenges": [], "rewrites": []})


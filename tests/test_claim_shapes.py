"""A shape is added and edited, never retired by absence, and frozen once claimed."""
from __future__ import annotations

import pytest

from app.core.errors import ClaimIsImmutable
from app.models.claims import (
    AuthoredClaimShape,
    ClaimImportance,
    DataUniverseRequirement,
    StageOutputCellCitation,
)
from app.models.records.claims import Claim, ClaimShape
from app.services import claim_shapes
from app.services.errors import ClaimShapesRefused

_PROJECT = "ai_lobbying"
# The two figures that run really published, with the coverage each one asserts.
_SPEND = AuthoredClaimShape(
    label="Paid to outside firms to lobby on AI, in dollars",
    requires=DataUniverseRequirement.closed, importance=ClaimImportance.primary,
)
_CORPUS = AuthoredClaimShape(
    label="Rows read across both exports", requires=DataUniverseRequirement.closed, importance=ClaimImportance.secondary,
)


def _claim(shape_id: str, value: float = 63027729.0) -> Claim:
    return Claim(
        project_id=_PROJECT, shape_id=shape_id,
        workflow_version_id="20260901T103742.393151",
        citation=StageOutputCellCitation(
            run_id="20260901T103753.789399", stage_id="ai_spend_totals",
            row_ordinal=0, column="ai_spend", value=value,
        ),
    )


# ── authoring ────────────────────────────────────────────────────────────────
def test_an_authored_shape_comes_back_with_an_id_and_what_it_covers():
    [stored] = claim_shapes.write_claim_shapes(_PROJECT, [_SPEND])

    assert (stored.label, stored.requires) == (_SPEND.label, "closed")
    assert stored.id


def test_the_primary_figure_is_listed_first():
    claim_shapes.write_claim_shapes(_PROJECT, [_CORPUS, _SPEND])

    assert [s.importance for s in claim_shapes.load_claim_shapes(_PROJECT)] == [
        "primary", "secondary"
    ]


def test_a_shape_of_another_project_is_not_found_by_id():
    [stored] = claim_shapes.write_claim_shapes(_PROJECT, [_SPEND])

    assert claim_shapes.load_claim_shape("venezuela_lda_lobbying", stored.id) is None


def test_a_shape_left_out_of_a_later_write_is_not_retired():
    """Absence cannot retire: a stage rule and every claim point at these by id."""
    claim_shapes.write_claim_shapes(_PROJECT, [_SPEND, _CORPUS])

    claim_shapes.write_claim_shapes(_PROJECT, [
        AuthoredClaimShape(label="Paying clients", requires=DataUniverseRequirement.open, importance=ClaimImportance.secondary)
    ])

    assert len(claim_shapes.load_claim_shapes(_PROJECT)) == 3


def test_an_id_edits_the_shape_it_names_rather_than_adding_another():
    [stored] = claim_shapes.write_claim_shapes(_PROJECT, [_SPEND])

    edited = claim_shapes.write_claim_shapes(_PROJECT, [
        AuthoredClaimShape(
            id=stored.id, label="Paid to outside firms to lobby on AI",
            requires=DataUniverseRequirement.closed, importance=ClaimImportance.primary,
        )
    ])

    assert [s.id for s in edited] == [stored.id]
    assert edited[0].label == "Paid to outside firms to lobby on AI"


# ── what is refused ──────────────────────────────────────────────────────────
def test_two_entries_sharing_a_label_are_refused_whole():
    with pytest.raises(ClaimShapesRefused, match="Rows read across both exports"):
        claim_shapes.write_claim_shapes(_PROJECT, [_CORPUS, _CORPUS])

    assert claim_shapes.load_claim_shapes(_PROJECT) == []


def test_a_label_this_project_already_claims_is_refused_without_its_id():
    claim_shapes.write_claim_shapes(_PROJECT, [_SPEND])

    with pytest.raises(ClaimShapesRefused, match="send that shape's id"):
        claim_shapes.write_claim_shapes(_PROJECT, [_SPEND])


def test_an_id_this_project_does_not_hold_is_refused():
    with pytest.raises(ClaimShapesRefused, match="holds no shape"):
        claim_shapes.write_claim_shapes(_PROJECT, [
            AuthoredClaimShape(
                id="d0e4d2a0", label="Paying clients", requires=DataUniverseRequirement.open, importance=ClaimImportance.secondary,
            )
        ])


def test_what_a_shape_covers_can_change_until_something_is_claimed_under_it():
    [stored] = claim_shapes.write_claim_shapes(_PROJECT, [_SPEND])
    widened = AuthoredClaimShape(
        id=stored.id, label=_SPEND.label, requires=DataUniverseRequirement.open, importance=ClaimImportance.primary,
    )

    assert claim_shapes.write_claim_shapes(_PROJECT, [widened])[0].requires == "open"


def test_what_a_shape_covers_freezes_once_it_is_claimed():
    [stored] = claim_shapes.write_claim_shapes(_PROJECT, [_SPEND])
    _claim(stored.id).save()

    with pytest.raises(ClaimShapesRefused, match="claimed 1 time"):
        claim_shapes.write_claim_shapes(_PROJECT, [
            AuthoredClaimShape(
                id=stored.id, label=_SPEND.label, requires=DataUniverseRequirement.open, importance=ClaimImportance.primary,
            )
        ])

    assert claim_shapes.load_claim_shapes(_PROJECT)[0].requires == "closed"


# ── a claim is written once ──────────────────────────────────────────────────
def test_saving_a_claim_a_second_time_raises():
    claim = _claim("some_shape")
    claim.save()

    with pytest.raises(ClaimIsImmutable):
        claim.save()


def test_a_claim_can_be_deleted_and_the_shape_claimed_again():
    claim = _claim("some_shape")
    claim.save()

    Claim.delete(claim.id)
    _claim("some_shape", value=1703875.0).save()

    assert [c.citation.value for c in Claim.find(shape_id="some_shape")] == [1703875.0]


def test_a_shape_is_only_a_shape_of_its_own_project():
    ClaimShape(
        project_id="venezuela_lda_lobbying", label="Filing rows read from the two exports",
        requires=DataUniverseRequirement.closed, importance=ClaimImportance.primary,
    ).save()
    claim_shapes.write_claim_shapes(_PROJECT, [_CORPUS])

    assert [s.label for s in claim_shapes.load_claim_shapes(_PROJECT)] == [_CORPUS.label]


# ── the Glossary page, which is the human's copy of what was agreed ──────────
def test_the_glossary_page_shows_what_the_project_claims_and_what_it_covers(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import workspace
    from app.services.methodology import write_methodology

    workspace.set_projects_dir(tmp_path)
    (tmp_path / "vocab").mkdir()
    write_methodology("vocab", "Follow the filings.")
    claim_shapes.write_claim_shapes("vocab", [_SPEND])

    response = TestClient(app).get("/project/vocab/glossary")

    assert response.status_code == 200
    assert _SPEND.label in response.text
    assert "Covers this is all of them." in response.text

"""A shape is added, never edited, and never retired by absence."""
from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.errors import ClaimIsImmutable, ClaimShapeIsImmutable
from app.models.claims import (
    ClaimImportance,
    ClaimShapeInput,
    ClaimStatus,
    DataUniverseRequirement,
    StageOutputCellCitation,
)
from app.models.records.claims import Claim, ClaimShape
from app.models.schema import Column
from app.services import claim_shapes
from app.services.errors import ClaimShapeWriteRefused

_PROJECT = "ai_lobbying"
# The two figures that run really published, with the coverage each one asserts.
_TIME = Column(name="period_start", type="date", nullable=False)
_SPEND = ClaimShapeInput(
    label="Reported by outside firms as received for AI lobbying in the United States, in dollars",
    requires=DataUniverseRequirement.closed, importance=ClaimImportance.primary,
    context=[_TIME],
    qualifiers=["Income no firm reported. Filing is required by law, so a firm that did "
                "not file is not in this figure."],
)
_CORPUS = ClaimShapeInput(
    label="Clients that paid a US outside firm for AI lobbying",
    requires=DataUniverseRequirement.closed, importance=ClaimImportance.secondary,
)


def _claim(shape_id: str, value: float = 63027729.0, claim_id: str = "") -> Claim:
    return Claim(
        id=claim_id or uuid4().hex,
        created_by_project_id=_PROJECT, shape_id=shape_id,
        citation=StageOutputCellCitation(
            run_id="20260901T103753.789399", stage_id="ai_spend_totals",
            row_ordinal=0, column="ai_spend", value=value,
        ),
    )


# ── authoring ────────────────────────────────────────────────────────────────
def test_an_authored_shape_comes_back_with_an_id_what_it_covers_and_its_qualifiers():
    [stored] = claim_shapes.write_claim_shapes(_PROJECT, [_SPEND])

    assert (stored.label, stored.requires) == (_SPEND.label, "closed")
    assert stored.qualifiers == _SPEND.qualifiers
    assert [column.name for column in stored.context] == ["period_start"]
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
        ClaimShapeInput(label="Paying clients", requires=DataUniverseRequirement.open, importance=ClaimImportance.secondary)
    ])

    assert len(claim_shapes.load_claim_shapes(_PROJECT)) == 3


# ── what is refused ──────────────────────────────────────────────────────────
def test_two_entries_sharing_a_label_are_refused_whole():
    with pytest.raises(ClaimShapeWriteRefused, match="Clients that paid a US outside firm"):
        claim_shapes.write_claim_shapes(_PROJECT, [_CORPUS, _CORPUS])

    assert claim_shapes.load_claim_shapes(_PROJECT) == []


def test_a_label_this_project_already_claims_is_refused():
    claim_shapes.write_claim_shapes(_PROJECT, [_SPEND])

    with pytest.raises(ClaimShapeWriteRefused, match="cannot be edited"):
        claim_shapes.write_claim_shapes(_PROJECT, [_SPEND])


def test_a_stored_shape_cannot_be_edited_at_all():
    """Every claim under a shape asserts what it said, so the words cannot move afterwards."""
    [stored] = claim_shapes.write_claim_shapes(_PROJECT, [_SPEND])

    with pytest.raises(ValidationError):
        stored.requires = DataUniverseRequirement.open


def test_a_shape_cannot_be_written_over_by_id():
    [stored] = claim_shapes.write_claim_shapes(_PROJECT, [_SPEND])

    with pytest.raises(ClaimShapeIsImmutable):
        ClaimShape(
            id=stored.id, project_id=_PROJECT, label="Something else",
            requires=DataUniverseRequirement.open, importance=ClaimImportance.primary,
        ).save()


# ── a claim is written once ──────────────────────────────────────────────────
def test_a_claims_status_is_the_only_thing_that_may_move():
    claim = _claim("some_shape")
    claim.save()

    claim.status = ClaimStatus.approved
    claim.save()

    assert Claim.load(claim.id).status == ClaimStatus.approved
    with pytest.raises(ValidationError):
        claim.shape_id = "another_shape"


def test_a_claim_cannot_be_written_over_by_id():
    claim = _claim("some_shape")
    claim.save()

    with pytest.raises(ClaimIsImmutable):
        _claim("a_different_shape", claim_id=claim.id).save()


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
def test_the_documentation_page_shows_what_the_project_claims_and_what_it_covers(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import workspace
    from app.services.methodology import write_methodology

    workspace.set_projects_dir(tmp_path)
    (tmp_path / "vocab").mkdir()
    write_methodology("vocab", "Follow the filings.")
    claim_shapes.write_claim_shapes("vocab", [_SPEND])

    response = TestClient(app).get("/project/vocab/methodology?tab=glossary")

    assert response.status_code == 200
    assert _SPEND.label in response.text
    assert ">closed</span>" in response.text
    assert "it IS this number." in response.text          # the tooltip explains the word
    assert "Income no firm reported." in response.text    # the qualifier is on the page


def test_a_template_naming_something_no_claim_can_fill_is_refused():
    """The page offers a sentence, so a placeholder with nothing behind it must not ship."""
    with pytest.raises(ClaimShapeWriteRefused, match=r"\['period'\]"):
        claim_shapes.write_claim_shapes(_PROJECT, [
            ClaimShapeInput(
                label="US lobbying spend on AI",
                requires=DataUniverseRequirement.closed,
                importance=ClaimImportance.primary,
                template="Firms reported ${value} for ${period}.",
                context=[Column(name="period_start", type="date", nullable=False)],
            )
        ])

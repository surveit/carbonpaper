from __future__ import annotations

from app.models.claims import ClaimImportance, ClaimShapeInput, DataUniverseRequirement
from app.models.records.claims import ClaimShape
from app.models.schema import Column

# The figure the Venezuela LDA project rests on, and the column it is read from.
_LABEL = "Total paid to outside lobbying firms to lobby on Venezuela"


def _shape(project_id: str, label: str = _LABEL) -> ClaimShape:
    return ClaimShape(
        project_id=project_id,
        label=label,
        universe=DataUniverseRequirement.closed,
        importance=ClaimImportance.primary,
    )


def test_a_shape_survives_the_store():
    saved = _shape("venezuela_lobbying_q1_q2_2026")
    saved.save()
    assert ClaimShape.load(saved.id).label == _LABEL


def test_shapes_are_found_by_their_project():
    _shape("venezuela_lobbying_q1_q2_2026").save()
    _shape("palm_oil_mill_register", "Mills in the register").save()
    found = ClaimShape.find(project_id="palm_oil_mill_register")
    assert [shape.label for shape in found] == ["Mills in the register"]


def test_a_stored_shape_reads_back_as_every_field_it_was_authored_with():
    authored = ClaimShapeInput(
        label=_LABEL, universe=DataUniverseRequirement.open,
        importance=ClaimImportance.secondary, qualifiers=["Q1 and Q2 filings only"],
        context=[Column(name="quarter", type="str", nullable=False)],
        template="Firms were paid {total} in {quarter}.")
    stored = ClaimShape(project_id="venezuela_lobbying_q1_q2_2026", **authored.model_dump())

    assert stored.read_input() == authored


def test_the_id_carries_nothing_of_the_record():
    shape = _shape("venezuela_lobbying_q1_q2_2026")
    assert "venezuela_lobbying_q1_q2_2026" not in shape.id
    assert _LABEL not in shape.id
    assert shape.id != _shape("venezuela_lobbying_q1_q2_2026").id

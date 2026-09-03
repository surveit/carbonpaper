from __future__ import annotations

from app.models.claims import ClaimImportance, DataUniverseRequirement
from app.models.records.claims import ClaimShape

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


def test_the_id_carries_nothing_of_the_record():
    shape = _shape("venezuela_lobbying_q1_q2_2026")
    assert "venezuela_lobbying_q1_q2_2026" not in shape.id
    assert _LABEL not in shape.id
    assert shape.id != _shape("venezuela_lobbying_q1_q2_2026").id

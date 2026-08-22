from __future__ import annotations

from app.models.claims import ClaimImportance, ClaimShape, DataUniverseRequirement

_LABEL = "Total paid to outside lobbying firms to lobby on Venezuela"


def _shape(label: str = _LABEL) -> ClaimShape:
    return ClaimShape(
        label=label,
        requires=DataUniverseRequirement.closed,
        importance=ClaimImportance.primary,
    )


def test_a_shape_survives_the_store():
    saved = _shape()
    saved.save()
    assert ClaimShape.load(saved.id).label == _LABEL


def test_shapes_are_found_by_their_label():
    _shape().save()
    _shape("Mills in the register").save()
    found = ClaimShape.find(label="Mills in the register")
    assert [shape.label for shape in found] == ["Mills in the register"]


def test_a_shape_belongs_to_no_project():
    assert "project_id" not in ClaimShape.model_fields


def test_the_id_carries_nothing_of_the_record():
    shape = _shape()
    assert _LABEL not in shape.id
    assert shape.id != _shape().id

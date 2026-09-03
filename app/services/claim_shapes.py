"""What a project claims, authored before any stage names one."""
from __future__ import annotations

from app.core.ids import ID
from app.models.claims import ClaimShapeInput
from app.models.records.claims import ClaimShape
from app.services.errors import ClaimShapeWriteRefused

_PRIMARY_FIRST = {"primary": 0, "secondary": 1}


def load_claim_shapes(project_id: ID) -> list[ClaimShape]:
    shapes = ClaimShape.find(project_id=project_id)
    return sorted(shapes, key=lambda shape: (_PRIMARY_FIRST[shape.importance], shape.label))


def load_claim_shape(project_id: ID, shape_id: ID) -> ClaimShape | None:
    stored = ClaimShape.load_or_none(shape_id)
    return stored if stored is not None and stored.project_id == project_id else None


def write_claim_shapes(project_id: ID, authored: list[ClaimShapeInput]) -> list[ClaimShape]:
    """Adds only. A shape is immutable, so a wrong one is retired and a new one written."""
    held = load_claim_shapes(project_id)
    refusals = find_claim_shape_refusals(authored, held)
    if refusals:
        raise ClaimShapeWriteRefused(refusals)
    for entry in authored:
        ClaimShape(
            project_id=project_id,
            label=entry.label,
            requires=entry.requires,
            importance=entry.importance,
            qualifiers=entry.qualifiers,
        ).save()
    return load_claim_shapes(project_id)


def find_claim_shape_refusals(
    authored: list[ClaimShapeInput], held: list[ClaimShape]
) -> list[str]:
    return [*_find_repeated_labels(authored), *_find_labels_taken(authored, held)]


def _find_repeated_labels(authored: list[ClaimShapeInput]) -> list[str]:
    labels = [entry.label for entry in authored]
    repeated = sorted({label for label in labels if labels.count(label) > 1})
    return [f"two shapes were sent with the label {label!r}" for label in repeated]


def _find_labels_taken(authored: list[ClaimShapeInput], held: list[ClaimShape]) -> list[str]:
    taken = {shape.label for shape in held}
    return [
        f"this project already claims {entry.label!r}, and a shape cannot be edited"
        for entry in authored
        if entry.label in taken
    ]

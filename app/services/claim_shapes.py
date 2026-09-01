"""What a project claims, authored before any stage names one."""
from __future__ import annotations

from app.core.ids import ID
from app.models.claims import AuthoredClaimShape
from app.models.records.claims import Claim, ClaimShape
from app.services.errors import ClaimShapesRefused

_PRIMARY_FIRST = {"primary": 0, "secondary": 1}


def load_claim_shapes(project_id: ID) -> list[ClaimShape]:
    shapes = ClaimShape.find(project_id=project_id)
    return sorted(shapes, key=lambda shape: (_PRIMARY_FIRST[shape.importance], shape.label))


def load_claim_shape(project_id: ID, shape_id: ID) -> ClaimShape | None:
    stored = ClaimShape.load_or_none(shape_id)
    return stored if stored is not None and stored.project_id == project_id else None


def write_claim_shapes(project_id: ID, authored: list[AuthoredClaimShape]) -> list[ClaimShape]:
    """Adds and edits; sending fewer retires none, because stages and claims point at these."""
    stored_by_id = {shape.id: shape for shape in load_claim_shapes(project_id)}
    refusals = find_claim_shape_refusals(project_id, authored, stored_by_id)
    if refusals:
        raise ClaimShapesRefused(refusals)
    for entry in authored:
        _write_one(project_id, entry, stored_by_id)
    return load_claim_shapes(project_id)


def find_claim_shape_refusals(
    project_id: ID, authored: list[AuthoredClaimShape], stored_by_id: dict[ID, ClaimShape]
) -> list[str]:
    return [
        *_find_repeated_labels(authored),
        *_find_labels_taken(authored, stored_by_id),
        *_find_unknown_ids(authored, stored_by_id),
        *_find_coverage_changes(project_id, authored, stored_by_id),
    ]


def _write_one(project_id: ID, entry: AuthoredClaimShape, stored_by_id: dict[ID, ClaimShape]) -> None:
    held = stored_by_id.get(entry.id) if entry.id is not None else None
    if held is None:
        ClaimShape(
            project_id=project_id,
            label=entry.label,
            requires=entry.requires,
            importance=entry.importance,
        ).save()
        return
    held.label = entry.label
    held.importance = entry.importance
    # Refused above once anything is claimed, so a published claim cannot be rewritten.
    held.requires = entry.requires
    held.save()


def _find_repeated_labels(authored: list[AuthoredClaimShape]) -> list[str]:
    labels = [entry.label for entry in authored]
    repeated = sorted({label for label in labels if labels.count(label) > 1})
    return [f"two shapes were sent with the label {label!r}" for label in repeated]


def _find_labels_taken(
    authored: list[AuthoredClaimShape], stored_by_id: dict[ID, ClaimShape]
) -> list[str]:
    by_label = {shape.label: shape_id for shape_id, shape in stored_by_id.items()}
    return [
        f"this project already claims {entry.label!r}; send that shape's id to edit it"
        for entry in authored
        if entry.label in by_label and by_label[entry.label] != entry.id
    ]


def _find_unknown_ids(
    authored: list[AuthoredClaimShape], stored_by_id: dict[ID, ClaimShape]
) -> list[str]:
    return [
        f"this project holds no shape {entry.id!r}; leave the id out to author a new one"
        for entry in authored
        if entry.id is not None and entry.id not in stored_by_id
    ]


def _find_coverage_changes(
    project_id: ID, authored: list[AuthoredClaimShape], stored_by_id: dict[ID, ClaimShape]
) -> list[str]:
    """What a claim asserts about coverage is fixed the moment anything is claimed under it."""
    changed = [
        (entry, entry.id) for entry in authored
        if entry.id is not None
        and entry.id in stored_by_id
        and stored_by_id[entry.id].requires != entry.requires
    ]
    claimed = [(entry, _count_claims(project_id, shape_id)) for entry, shape_id in changed]
    return [
        f"{entry.label!r} has been claimed {count} time(s), so what it covers "
        f"cannot change to {entry.requires}"
        for entry, count in claimed if count
    ]


def _count_claims(project_id: ID, shape_id: ID) -> int:
    return len(Claim.find(project_id=project_id, shape_id=shape_id))

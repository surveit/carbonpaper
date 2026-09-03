"""The claim-shape tool bodies; app.tools.shared sits at its import ceiling."""
from __future__ import annotations

from app.models.claims import ClaimShapeInput
from app.models.records.claims import ClaimShape
from app.services import claim_shapes as claim_shapes_service
from app.tools.shared import validate_project_exists


def read_claim_shapes(project_id: str) -> list[ClaimShape]:
    validate_project_exists(project_id)
    return claim_shapes_service.load_claim_shapes(project_id)


def write_claim_shapes(project_id: str, shapes: list[ClaimShapeInput]) -> list[ClaimShape]:
    validate_project_exists(project_id)
    return claim_shapes_service.write_claim_shapes(project_id, shapes)

"""One finished scope-fixture run with one claim on its total, for the review tests."""
from __future__ import annotations

from app.models.claims import (
    ClaimImportance, ClaimShapeInput, DataUniverseRequirement, StageOutputCellCitation,
)
from app.models.records.claims import Claim
from app.models.records.workflow_output import WorkflowOutput
from app.services import claim_shapes, claims
from app.services import run as run_service
from app.services.project import save_working_copy_as_version
from scope_fixture import stage_specs, write_inputs
from stage_seed import set_stages

PROJECT = "scope_fixture"
TOTAL_TEXT = "Grants came to 2,200 in total."
TOTAL_SHAPE = ClaimShapeInput(
    label="What the grants came to, in whole units",
    universe=DataUniverseRequirement.closed, importance=ClaimImportance.primary)


def run_the_fixture(projects_root) -> str:
    data = projects_root / PROJECT / "data"
    write_inputs(data)
    set_stages(PROJECT, stage_specs(data))
    save_working_copy_as_version(PROJECT, message="fixture")
    return str(run_service.execute(PROJECT)["run_id"])


def claim_the_total(run_id: str, text: str = TOTAL_TEXT) -> Claim:
    [shape] = claim_shapes.write_claim_shapes(PROJECT, [TOTAL_SHAPE])
    WorkflowOutput(
        slug="grant-total", label="What the grants came to", primary=True, shape_id=shape.id,
        citation=StageOutputCellCitation(
            run_id=run_id, stage_id="grant_totals", row_ordinal=0,
            column="total_amount", value=2200),
    ).save()
    WorkflowOutput(
        slug="grant-count", label="How many grants", primary=False, shape_id=None,
        citation=StageOutputCellCitation(
            run_id=run_id, stage_id="grant_totals", row_ordinal=0, column="grants", value=5),
    ).save()
    return claims.submit_claim(PROJECT, run_id, "grant-total", {}, text)

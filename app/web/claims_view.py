"""The publish page: what a run could claim, what it has claimed, and in what words."""
from __future__ import annotations

from string import Template

from pydantic import BaseModel

from app.core.ids import ID
from app.core.json_types import JsonDict
from app.core.run_status import RunStatus
from app.models.claims import (
    ClaimStatus,
    DataUniverseRequirement,
    PublishedCitation,
    StageOutputCellCitation,
)
from app.models.records.claims import Claim, ClaimShape
from app.models.records.workflow_output import WorkflowOutput
from app.models.schema import Column
from app.runtime.citations import build_row_trace_url
from app.services import claims as claims_service
from app.services.claim_shapes import load_claim_shapes
from app.web.run_index import RunIndexRow
from app.web.run_published import render_output_value


class ContextField(BaseModel):
    """One column of the shape's context, and what to put in the form for it."""

    name: str
    input_type: str
    value: str


class StandingClaim(BaseModel):
    text: str
    value: str
    href: str
    run_href: str
    status: str
    # The context members this claim holds that the one being written does not.
    differs_by: list[str]


class ClaimCard(BaseModel):
    slug: str
    metric: str
    value: str
    href: str
    coverage: str
    coverage_tooltip: str
    qualifiers: list[str]
    context: list[ContextField]
    suggested_text: str
    # Set once a claim exists for this output; the card then shows its state.
    claim_id: ID | None
    status: str | None
    text: str
    blocked: str | None
    restates: bool
    standing: list[StandingClaim]


class ClaimCounts(BaseModel):
    claimed: int
    in_review: int
    skipped: int
    waiting: int
    blocked: int


class PublishView(BaseModel):
    run_id: ID
    counts: ClaimCounts
    cards: list[ClaimCard]


COVERAGE_TOOLTIP = {
    DataUniverseRequirement.closed: "The dataset holds every event of this kind, so this "
                                    "figure is the total: it IS this number.",
    DataUniverseRequirement.open: "The dataset holds only the events it captured, so this "
                                  "figure is a floor: the real number is AT LEAST this.",
}
_INPUT_TYPES = {"date": "date", "datetime": "datetime-local", "int": "number",
                "float": "number"}


def build_publish_view(project_id: ID, run: RunIndexRow) -> PublishView:
    shapes = {shape.id: shape for shape in load_claim_shapes(project_id)}
    held = claims_service.load_run_claims(project_id, run.run_id)
    blocked = describe_what_blocks_the_run(run)
    cards = [
        _build_card(project_id, output, shapes[output.shape_id], held, blocked)
        for output in claims_service.read_workflow_run_outputs(run.run_id)
        if output.shape_id in shapes
    ]
    return PublishView(run_id=run.run_id, counts=_count(cards), cards=cards)


def describe_what_blocks_the_run(run: RunIndexRow) -> str:
    """Empty when nothing does. A closed metric needs the whole input; an open one does not."""
    if run.status != RunStatus.OK:
        return f"This run ended {run.status}, so its numbers are not the answer."
    if run.is_test_run:
        return "This was a test run, so it read a window of the rows."
    if run.stage_caps:
        named = ", ".join(f"{cap.stage_id} (first {cap.cap:,})" for cap in run.stage_caps)
        return f"{named} read a window of its input."
    return ""


def read_whether_the_run_read_everything(run: RunIndexRow) -> bool:
    return not describe_what_blocks_the_run(run)


def _build_card(
    project_id: ID,
    output: WorkflowOutput,
    shape: ClaimShape,
    held_by_shape_id: dict[ID, Claim],
    blocked: str,
) -> ClaimCard:
    claim = held_by_shape_id.get(shape.id)
    standing_claims = claims_service.load_claims_of_shape(project_id, shape.id)
    context = _build_context(shape, claim, standing_claims)
    proposed = {field.name: field.value for field in context}
    standing = [
        _build_standing(project_id, other, proposed)
        for other in standing_claims
        if claim is None or other.id != claim.id
    ]
    return ClaimCard(
        slug=output.slug,
        metric=shape.label,
        value=_read_value(output.citation),
        href=build_row_trace_url(
            project_id, output.citation.run_id, output.citation.stage_id, 0
        ),
        coverage=shape.requires,
        coverage_tooltip=COVERAGE_TOOLTIP[DataUniverseRequirement(shape.requires)],
        qualifiers=shape.qualifiers,
        context=context,
        suggested_text=_write_the_suggestion(shape.template, output.citation, proposed),
        claim_id=claim.id if claim else None,
        status=claim.status if claim else None,
        text=claim.text if claim else "",
        blocked=_find_blocked_reason(shape, blocked),
        restates=any(not other.differs_by for other in standing),
        standing=standing,
    )


def _read_value(cited: PublishedCitation) -> str:
    """A table citation names rows, not one value; the page shows its row count instead."""
    if isinstance(cited, StageOutputCellCitation):
        return render_output_value(cited.value)
    return f"{cited.rectangle.count_rows():,} rows"


def _find_blocked_reason(shape: ClaimShape, blocked: str) -> str | None:
    if not blocked or shape.requires == DataUniverseRequirement.open:
        return None
    return f"{blocked} Totalling a slice turns 'is' into 'at least'."


def _write_the_suggestion(template: str, cited: PublishedCitation, context: JsonDict) -> str:
    """Filled in, never offered with the placeholders showing: it is a sentence to edit."""
    return Template(template).safe_substitute(value=_read_value(cited), **context)


def _build_context(
    shape: ClaimShape, claim: Claim | None, standing: list[Claim]
) -> list[ContextField]:
    """A rebuild usually keeps every column but the period, so the newest claim pre-fills."""
    held = claim.context if claim else (standing[-1].context if standing else {})
    return [_build_field(column, held) for column in shape.context]


def _build_field(column: Column, held: JsonDict) -> ContextField:
    value = held.get(column.name, "")
    return ContextField(
        name=column.name,
        input_type=_INPUT_TYPES.get(column.type, "text"),
        value="" if value is None else str(value),
    )


def _build_standing(project_id: ID, claim: Claim, proposed: JsonDict) -> StandingClaim:
    cited = claim.citation
    return StandingClaim(
        text=claim.text,
        value=_read_value(cited),
        href=build_row_trace_url(project_id, cited.run_id, cited.stage_id, 0),
        run_href=f"/project/{project_id}/runs/{cited.run_id}",
        status=claim.status,
        differs_by=[
            f"{name} {value}"
            for name, value in claim.context.items()
            if str(proposed.get(name, "")) != str(value)
        ],
    )


def _count(cards: list[ClaimCard]) -> ClaimCounts:
    return ClaimCounts(
        claimed=len([c for c in cards if c.status == ClaimStatus.approved]),
        in_review=len([c for c in cards if c.status == ClaimStatus.submitted]),
        skipped=len([c for c in cards if c.status == ClaimStatus.declined]),
        blocked=len([c for c in cards if c.blocked and c.status is None]),
        waiting=len([c for c in cards if c.status is None and not c.blocked]),
    )

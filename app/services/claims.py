"""A claim's lifecycle: proposing one, and the checks that decide whether it may stand."""
from __future__ import annotations

from pydantic import ValidationError

from app.core.ids import ID
from app.core.json_types import JsonDict
from app.models.claims import ClaimStatus, DataUniverseRequirement
from app.models.records.claims import Claim, ClaimShape
from app.models.records.workflow_output import WorkflowOutput
from app.models.schema import TableSchema
from app.services.claim_shapes import load_claim_shape
from app.services.errors import ClaimRefused


def submit_claim(
    project_id: ID, run_id: ID, slug: str, context: JsonDict, text: str
) -> Claim:
    """Proposed, not made: it stands behind nothing until review approves it."""
    output = read_workflow_run_output_by_slug(run_id, slug)
    shape = _require_shape(project_id, output.shape_id)
    held = read_context(shape, context)
    validate_nothing_equivalent_stands(project_id, shape, held)
    claim = Claim(
        created_by_project_id=project_id, shape_id=shape.id,
        context=held, citation=output.citation, text=_require_text(text),
    )
    claim.save()
    return claim


def approve_claim(project_id: ID, claim_id: ID, run_read_everything: bool) -> Claim:
    """Re-checked before it stands: what was in the way can move while it waits."""
    claim = load_claim(project_id, claim_id)
    shape = _require_shape(project_id, claim.shape_id)
    validate_run_covers_the_shape(shape, run_read_everything)
    validate_nothing_equivalent_stands(project_id, shape, claim.context, besides_claim_id=claim.id)
    return _set_status(claim, ClaimStatus.approved)


def decline_claim(project_id: ID, claim_id: ID) -> Claim:
    """The claim stays, saying so: a figure refused reads differently from one never opened."""
    return _set_status(load_claim(project_id, claim_id), ClaimStatus.declined)


def decline_output(project_id: ID, run_id: ID, slug: str) -> Claim:
    """A skip: proposed and refused in one act, so the run's counts still add up."""
    output = read_workflow_run_output_by_slug(run_id, slug)
    shape = _require_shape(project_id, output.shape_id)
    claim = Claim(
        created_by_project_id=project_id, shape_id=shape.id,
        citation=output.citation, status=ClaimStatus.declined,
    )
    claim.save()
    return claim


# ─── reading ──────────────────────────────────────────────────────────────────


def load_claim(project_id: ID, claim_id: ID) -> Claim:
    held = Claim.load_or_none(claim_id)
    if held is None or held.created_by_project_id != project_id:
        raise ClaimRefused([f"this project holds no claim '{claim_id}'"])
    return held


def load_claims_of_shape(project_id: ID, shape_id: ID) -> list[Claim]:
    """Standing or awaiting review, oldest first. A declined claim is nobody's history."""
    claims = Claim.find(created_by_project_id=project_id, shape_id=shape_id)
    return sorted(
        (claim for claim in claims if claim.status != ClaimStatus.declined),
        key=lambda claim: claim.created_at,
    )


def load_run_claims(project_id: ID, run_id: ID) -> dict[str, Claim]:
    """This run's own claim per shape. The citation carries the run; find() cannot select on it."""
    return {
        claim.shape_id: claim
        for claim in Claim.find(created_by_project_id=project_id)
        if claim.citation.run_id == run_id
    }


def read_workflow_run_outputs(run_id: ID) -> list[WorkflowOutput]:
    """The run's outputs that name a shape. Naming none is ordinary, and claims nothing."""
    return [
        output for output in WorkflowOutput.list()
        if output.shape_id is not None and output.citation.run_id == run_id
    ]


def read_workflow_run_output_by_slug(run_id: ID, slug: str) -> WorkflowOutput:
    for output in read_workflow_run_outputs(run_id):
        if output.slug == slug:
            return output
    raise ClaimRefused([f"run '{run_id}' published no claimable output '{slug}'"])


def find_equivalent_claims(
    project_id: ID, shape_id: ID, context: JsonDict, besides_claim_id: ID = ""
) -> list[Claim]:
    """Same shape, same context: the same fact, so two of them cannot both be right."""
    return [
        claim for claim in load_claims_of_shape(project_id, shape_id)
        if claim.id != besides_claim_id and claim.context == context
    ]




# ─── what stands in the way ───────────────────────────────────────────────────


def read_context(shape: ClaimShape, context: JsonDict) -> JsonDict:
    """As JSON: a date and the string spelling it must be one context, not two."""
    try:
        held = TableSchema(columns=shape.context).to_pydantic_model("Context").model_validate(
            context
        )
    except ValidationError as exc:
        raise ClaimRefused([f"{err['loc']}: {err['msg']}" for err in exc.errors()]) from exc
    return held.model_dump(mode="json")


def validate_run_covers_the_shape(shape: ClaimShape, run_read_everything: bool) -> None:
    if shape.requires == DataUniverseRequirement.closed and not run_read_everything:
        raise ClaimRefused([
            f"{shape.label!r} is closed, and this run read a window — "
            "totalling a slice turns 'is' into 'at least'"
        ])


def validate_nothing_equivalent_stands(
    project_id: ID, shape: ClaimShape, context: JsonDict, besides_claim_id: ID = ""
) -> None:
    equivalent = find_equivalent_claims(project_id, shape.id, context, besides_claim_id)
    if equivalent:
        raise ClaimRefused([
            f"this restates claim {claim.id} — same context, and it still stands"
            for claim in equivalent
        ])


def learn_the_template(project_id: ID, shape_id: ID, template: str) -> ClaimShape:
    """The template asserts nothing, so a claim that read better can rewrite it."""
    shape = _require_shape(project_id, shape_id)
    shape.template = template.strip()
    shape.save()
    return shape


def _require_shape(project_id: ID, shape_id: ID | None) -> ClaimShape:
    shape = load_claim_shape(project_id, shape_id) if shape_id else None
    if shape is None:
        raise ClaimRefused([f"this project holds no claim shape '{shape_id}'"])
    return shape


def _require_text(text: str) -> str:
    if not text.strip():
        raise ClaimRefused(["a claim is the sentence someone will publish; write it"])
    return text.strip()


def _set_status(claim: Claim, status: ClaimStatus) -> Claim:
    claim.status = status
    claim.save()
    return claim

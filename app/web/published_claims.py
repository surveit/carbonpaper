"""The figures a run declared as its answer, addressed at whichever surface renders them."""
from __future__ import annotations

from pydantic import BaseModel

from app.core.json_types import JsonScalar
from app.models.records.workflow_output import WorkflowOutput
from app.web.panel_links import PanelLinks


class PublishedClaim(BaseModel):
    """`href` is None where the surface holds no page for the row behind the value."""

    label: str
    value: str
    href: str | None


class PublishedClaims(BaseModel):
    leads: list[PublishedClaim]
    rest: list[PublishedClaim]

    def any(self) -> bool:
        return bool(self.leads or self.rest)

    def any_traced(self) -> bool:
        return any(claim.href for claim in [*self.leads, *self.rest])


def read_published_claims(run_id: str, links: PanelLinks) -> PublishedClaims:
    """Filtered in python: the run id sits inside the citation, which find() cannot select on."""
    published = sorted(
        (output for output in WorkflowOutput.list() if output.citation.run_id == run_id),
        key=lambda output: output.slug,
    )
    return PublishedClaims(
        leads=[_build_claim(links, o) for o in published if o.primary],
        rest=[_build_claim(links, o) for o in published if not o.primary],
    )


def render_output_value(value: JsonScalar) -> str:
    """A null reads as absent rather than as the word None."""
    return "—" if value is None else f"{value:,}" if isinstance(value, (int, float)) else str(value)


def _build_claim(links: PanelLinks, output: WorkflowOutput) -> PublishedClaim:
    citation = output.citation
    return PublishedClaim(
        label=output.label,
        value=render_output_value(citation.value),
        href=links.claim_trace(citation.stage_id, citation.row_ordinal, citation.column),
    )

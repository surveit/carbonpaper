"""The figures a run published, addressed at the packet's own lineage pages."""
from __future__ import annotations

from pydantic import BaseModel

from app.models.records.workflow_output import WorkflowOutput
from app.web.panel_links import PacketPanelLinks
from app.web.run_header import render_output_value


class PacketClaim(BaseModel):
    """`href` is None where this packet holds no lineage page for the row behind it."""

    label: str
    value: str
    href: str | None


class PacketClaims(BaseModel):
    leads: list[PacketClaim]
    rest: list[PacketClaim]

    def any_traced(self) -> bool:
        return any(claim.href for claim in [*self.leads, *self.rest])


def build_packet_claims(run_id: str, traced: frozenset[tuple[str, int]]) -> PacketClaims:
    links = PacketPanelLinks(to_root="", traced=traced)
    published = sorted(
        (output for output in WorkflowOutput.list() if output.citation.run_id == run_id),
        key=lambda output: output.slug,
    )
    return PacketClaims(
        leads=[_build_claim(links, o) for o in published if o.primary],
        rest=[_build_claim(links, o) for o in published if not o.primary],
    )


def _build_claim(links: PacketPanelLinks, output: WorkflowOutput) -> PacketClaim:
    citation = output.citation
    return PacketClaim(
        label=output.label,
        value=render_output_value(citation.value),
        href=links.row_trace(citation.stage_id, citation.row_ordinal),
    )

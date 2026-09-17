"""The URL each citation kind opens at, so a reader can check it without the review."""
from __future__ import annotations

from app.models.citations import (
    AddressedChallengeCitation,
    AddressedStageCitation,
    AddressedStageOutputCellCitation,
    AddressedStageOutputColumnCitation,
    AddressedTermCitation,
)


def render_source_url(citation: AddressedChallengeCitation) -> str:
    if isinstance(citation, AddressedStageOutputCellCitation):
        return (f"/project/{citation.project_id}/runs/{citation.run_id}"
                f"/stage/{citation.stage_id}/row/{citation.row_ordinal}/trace")
    if isinstance(citation, AddressedStageOutputColumnCitation):
        return (f"/project/{citation.project_id}/runs/{citation.run_id}"
                f"/stage/{citation.stage_id}/preview#column-{citation.column}")
    if isinstance(citation, AddressedStageCitation):
        return f"/project/{citation.project_id}/node/{citation.stage_id}/panel"
    if isinstance(citation, AddressedTermCitation):
        # No per-term page yet, so the glossary's own anchor for the noun.
        return f"/project/{citation.project_id}/methodology#noun-{citation.name}"
    raise ValueError(f"no source URL for a {citation.kind} citation")

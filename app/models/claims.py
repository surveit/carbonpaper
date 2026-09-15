"""What a project intends to claim, and what one run established."""
from __future__ import annotations

from enum import Enum

from app.models.schema import Column, _Base


class ClaimImportance(str, Enum):
    primary = "primary"
    secondary = "secondary"


class ClaimStatus(str, Enum):
    """Proposed, then stood behind, refused, or replaced. The only field that may move."""

    submitted = "submitted"
    approved = "approved"
    declined = "declined"
    superseded = "superseded"


class DataUniverseRequirement(str, Enum):
    open = "open"
    closed = "closed"


# The word is the fact and goes on the page; this explains it on hover.
DATA_UNIVERSE_TOOLTIP = {
    "open": "The dataset holds only the events it captured, so this figure is a floor: "
            "the real number is AT LEAST this.",
    "closed": "The dataset holds every event of this kind, so this figure is the total: "
              "it IS this number.",
}



class ClaimShapeInput(_Base):
    """What a caller sends to author one shape. A stored shape is never edited, so no id."""

    label: str
    universe: DataUniverseRequirement
    importance: ClaimImportance
    qualifiers: list[str] = []
    # The axes a claim of this shape sits on, as ordinary columns.
    context: list[Column] = []
    # The sentence a claim's own words are suggested from; it asserts nothing.
    template: str = ""


"""One step of a review guide's walkthrough. The guide itself is a stored record
(`app.services.versioning.ReviewGuide`), which embeds these.
"""
from __future__ import annotations

from app.models.schema import _Base


class ReviewGuideStep(_Base):
    """One step of the walkthrough. `prose` may carry `backticked` column names."""

    title: str
    prose: str
    stage_ids: list[str]

"""Persisting what a run stated. The runtime mints a claim; nothing below here writes one."""
from __future__ import annotations

from collections.abc import Iterable

from app.models.claims import Claim


def save_claims(claims: Iterable[Claim]) -> None:
    for claim in claims:
        claim.save()

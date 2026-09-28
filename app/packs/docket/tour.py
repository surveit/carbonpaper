"""The Boeing chronology bundle, read offline from the RECAP mirror committed beside it."""
from __future__ import annotations

from pathlib import Path

from app.models.packs import TourFixture

_SEEDS_DATA = Path(__file__).resolve().parents[2] / "seeds" / "data"
BOEING_MIRROR = _SEEDS_DATA / "boeing"

BOEING_TOUR = TourFixture(
    bundle=_SEEDS_DATA / "boeing_docket_chronology.json",
    bindings={"input_filings": {"cache_dir": str(BOEING_MIRROR)}},
)

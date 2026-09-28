"""The docket pack: a federal court docket's filings, read from CourtListener's RECAP archive."""
from __future__ import annotations

from app.models.packs import PackSpec, register_pack
from app.packs.docket.connector import RECAP_DOCKET
from app.packs.docket.tour import BOEING_TOUR

register_pack(PackSpec(pack_id="docket", connectors=(RECAP_DOCKET,), tour=BOEING_TOUR))

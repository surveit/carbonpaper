"""The opinions pack: appeals court opinions a CourtListener search finds, read as stored PDFs."""
from __future__ import annotations

from app.models.packs import PackSpec, register_pack
from app.packs.opinions.connector import COURTLISTENER_OPINIONS

register_pack(PackSpec(pack_id="opinions", connectors=(COURTLISTENER_OPINIONS,)))

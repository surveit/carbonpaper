"""The web pack: pages and documents a list of URLs names, read as stored sources."""
from __future__ import annotations

from app.models.packs import PackSpec, register_pack
from app.packs.web.connector import WEB_PAGES

register_pack(PackSpec(pack_id="web", connectors=(WEB_PAGES,)))

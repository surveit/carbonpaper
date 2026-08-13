"""The ⌘K bar's one GET: the whole index as JSON, ranked, for the browser to filter.

Under its own prefix rather than beside the things it lists, for the reason
/pickers has one: /project/{project}/... would match a listing path as a project id.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.web.cmdk_palette import CmdkPaletteIndex, build_cmdk_palette_index

router = APIRouter()


@router.get("/cmdk_palette/index")
async def cmdk_palette_index(project: str = "", run: str = "") -> CmdkPaletteIndex:
    return build_cmdk_palette_index(project, run)

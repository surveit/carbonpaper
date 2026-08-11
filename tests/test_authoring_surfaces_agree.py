"""Both authoring surfaces — the editing agent and the MCP server — carry every
shared piece of authoring guidance. They are edited separately, so a rule added to
one and forgotten on the other is the failure this pins."""
from __future__ import annotations

import re

import pytest

from app.tools.prompt_fragments import (
    HANDOVER_BARS_NOTE,
    HOW_YOU_WORK_NOTE,
    REVIEW_GUIDE_NOTE,
)
from app.models.authoring_lifecycle_note import AUTHORING_LIFECYCLE_GUIDANCE
from app.tools.prompt_fragments import ENUM_FROM_DATA_GUIDANCE
from app.tools.prompt_fragments import CONCEPTS_NOTE, ROLE_NOTE
from app.tools.prompt_fragments import render_stage_anatomy, render_type_catalog
from app.models.stages.code import (
    CODE_CORNER_CASES_CONTRACT_NOTE,
    CODE_SUMMARY_CONTRACT_NOTE,
)
from app.models.stages.stage_types import AUTHORABLE_TYPES
from app.models.stages.signature import SIGNATURE_CONTRACT_NOTE
from app.tools.prompt_fragments import WORKED_STAGE_EXAMPLE

# Everything an author needs wherever they author from. A surface-specific tool
# walkthrough is not here; a RULE about what a stage may be always is.
SHARED_GUIDANCE = {
    "role": ROLE_NOTE,
    "concepts": CONCEPTS_NOTE,
    "how_you_work": HOW_YOU_WORK_NOTE,
    "review_guide": REVIEW_GUIDE_NOTE,
    "handover_bars": HANDOVER_BARS_NOTE,
    "lifecycle": AUTHORING_LIFECYCLE_GUIDANCE,
    "enum_from_data": ENUM_FROM_DATA_GUIDANCE,
    "stage_anatomy": render_stage_anatomy(),
    "signature_contract": SIGNATURE_CONTRACT_NOTE,
    "code_budget": CODE_SUMMARY_CONTRACT_NOTE,
    "corner_cases": CODE_CORNER_CASES_CONTRACT_NOTE,
    "worked_example": WORKED_STAGE_EXAMPLE,
}


@pytest.fixture(scope="module")
def surfaces() -> dict[str, str]:
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT
    from app.mcp.server import INSTRUCTIONS

    return {"editing agent": EDITING_SYSTEM_PROMPT, "mcp server": INSTRUCTIONS}


@pytest.mark.parametrize("name", sorted(SHARED_GUIDANCE))
def test_shared_guidance_reaches_both_surfaces(name: str, surfaces: dict[str, str]) -> None:
    wanted = _flat(SHARED_GUIDANCE[name])
    missing = [surface for surface, text in surfaces.items() if wanted not in _flat(text)]
    assert not missing, f"`{name}` is missing from: {', '.join(missing)}"


def test_the_type_catalog_is_rendered_identically_on_both_surfaces(
    surfaces: dict[str, str],
) -> None:
    # Not just present — the same text, blocks and signature form included.
    catalog = _flat(render_type_catalog())
    missing = [s for s, text in surfaces.items() if catalog not in _flat(text)]
    assert not missing, f"type catalog differs on: {', '.join(missing)}"


def test_every_authorable_type_names_its_block_and_signature_form(
    surfaces: dict[str, str],
) -> None:
    for stage_type, spec in AUTHORABLE_TYPES.items():
        for surface, text in surfaces.items():
            assert f"- {stage_type} —" in text, f"{stage_type} missing from {surface}"
        assert f"signature form: {spec.signature_form}" in _flat(render_type_catalog())


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def test_the_mcp_instructions_place_every_tool_it_offers() -> None:
    # Its instructions are the only thing saying WHEN to call each tool.
    import asyncio

    from app.mcp.instructions import INSTRUCTIONS
    from app.mcp.server import mcp

    tools = asyncio.run(mcp.list_tools())
    unplaced = sorted(t.name for t in tools if t.name not in INSTRUCTIONS)
    assert not unplaced, f"offered but never placed in a phase: {unplaced}"

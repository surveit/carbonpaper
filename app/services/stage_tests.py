"""Services-layer seam onto the stage-test agents.

The deriver and code-repair agent BUILDERS live in app.compiler.stage_tests (they configure
app.core.agent Agents from the compiler's prompts). The generation-time pipeline that drives
them, app.web.stage_test_derivation, also needs app.runtime to RUN the derived tests — a pairing
no single lower layer is allowed to hold, so the pipeline sits in app.web. But app.web may not
import app.compiler (only app.main and app.services may). This thin service is that one permitted
hop: it re-exports the two builders unchanged, so the web pipeline reaches the compiler through
the services layer it is allowed to import. No logic lives here — the builders are the contract.
"""
from __future__ import annotations

from app.compiler.stage_tests import (
    RepairedStageCode,
    build_stage_test_deriver,
    build_stage_test_repair_agent,
)

__all__ = [
    "RepairedStageCode",
    "build_stage_test_deriver",
    "build_stage_test_repair_agent",
]

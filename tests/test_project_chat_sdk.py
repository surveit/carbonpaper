"""Importing app.agents.compiler.config for its side effect is what registers the
"editing" agent; engine construction is lazy, so no project need exist on disk.
"""
from __future__ import annotations

import app.agents.compiler.config  # noqa: F401 — registers the "editing" agent
from app.core.agent.registry import build_engine
from app.core.agent.sdk_engine import ClaudeAgentSdkEngine


def test_build_editing_engine_returns_correct_type() -> None:
    engine = build_engine("editing", {"project_id": "congresswatch", "base_url": "http://reader.test/"})
    assert isinstance(engine, ClaudeAgentSdkEngine)

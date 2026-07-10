"""Tests for building the editing agent's engine through the registry.

Importing app.compiler.agent.config registers the "editing" agent; build_engine
then validates the opaque context against EditingContext, builds that project's
tools, wraps them, and returns a ClaudeAgentSdkEngine. Construction is lazy w.r.t.
the filesystem (the tools only bind the project name), so this constructs an
engine for a project without seeding a project directory on disk.
"""
from __future__ import annotations

import app.compiler.agent.config  # noqa: F401 — registers the "editing" agent
from app.agent.registry import build_engine
from app.agent.sdk_engine import ClaudeAgentSdkEngine


def test_build_editing_engine_returns_correct_type() -> None:
    engine = build_engine("editing", {"project_id": "congresswatch"})
    assert isinstance(engine, ClaudeAgentSdkEngine)

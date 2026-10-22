"""Importing app.agents.compiler.config for its side effect is what registers the
"editing" agent; engine construction is lazy, so no project need exist on disk.
"""
from __future__ import annotations

import app.agents.compiler.config  # noqa: F401 — registers the "editing" agent
from app.core.agent.registry import build_engine


def test_the_editing_engine_builds_for_a_project_not_on_disk() -> None:
    build_engine("editing", {"project_id": "congresswatch", "base_url": "http://reader.test/"})

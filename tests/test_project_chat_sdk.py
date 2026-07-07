"""Tests for mounting the subscription SDK engine on project chat sessions.

`get_project_sdk_engine(name)` builds one SdkAgentEngine per project and caches
it. Construction must be lazy w.r.t. the filesystem (the tool closures only bind
`examples_dir / name`; nothing is read at build time), so this test constructs
an engine for a project without seeding a project directory on disk.
"""
from __future__ import annotations

from app.chat.project_agent import get_project_sdk_engine
from app.chat.sdk_engine import SdkAgentEngine


def test_project_sdk_engine_is_cached_and_correct_type() -> None:
    a = get_project_sdk_engine("congresswatch")
    b = get_project_sdk_engine("congresswatch")
    assert a is b
    assert isinstance(a, SdkAgentEngine)

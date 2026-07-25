"""Importing app.core.llm_sdk must leave this process's environment free of the
markers that make a spawned `claude` CLI look like a NESTED invocation. Every
CLI-spawning path imports this module, so the strip has to happen here for the
child process to start at all when the server itself runs inside Claude Code."""
from __future__ import annotations

import importlib
import os

import pytest

NESTED_SESSION_MARKERS = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_EXECPATH",
    "AI_AGENT",
)


def test_llm_sdk_import_strips_nested_session_markers(monkeypatch: pytest.MonkeyPatch):
    for marker in NESTED_SESSION_MARKERS:
        monkeypatch.setenv(marker, "set-by-the-enclosing-session")

    import app.core.llm_sdk

    importlib.reload(app.core.llm_sdk)

    still_set = [m for m in NESTED_SESSION_MARKERS if m in os.environ]
    assert not still_set, f"markers survived the import: {still_set}"

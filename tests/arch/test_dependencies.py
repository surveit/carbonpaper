"""Architecture: the legacy pydantic-ai engine stays gone.

The chat/agent stack runs on claude-agent-sdk (app.runtime.llm_agent_sdk). No module
under app/ may import pydantic_ai, so a stray re-introduction fails CI instead of
quietly reviving a second engine.
"""
from __future__ import annotations

from arch._helpers import collect_imports, iter_module_files, parse_module

_BANNED_TOP_LEVEL = {"pydantic_ai"}


def test_app_does_not_import_pydantic_ai() -> None:
    offenders: list[str] = []
    for path in iter_module_files(""):
        tree = parse_module(path)
        banned = {
            module
            for module in collect_imports(tree)
            if module.split(".", 1)[0] in _BANNED_TOP_LEVEL
        }
        if banned:
            offenders.append(f"{path}: {sorted(banned)}")
    assert not offenders, (
        "pydantic_ai must not be imported in app/ (migrated to claude-agent-sdk):\n  "
        + "\n  ".join(offenders)
    )

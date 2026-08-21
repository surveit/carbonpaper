"""Architecture: the rail and the panel hold a session id, never an agent's name."""
from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP = _REPO_ROOT / "app"
_AGENTS = _APP / "agents"

# index.html is absent on purpose: a landing page may advertise an agent.
_GOVERNED = (
    "web/chat_router.py",
    "templates/_chat_panel.html",
    "templates/_chat_rail.html",
    "templates/_chat_rail_head.html",
    "templates/base.html",
    "templates/lineage.html",
    "static/chat-panel.js",
    "static/chat-rail.js",
    "static/chat-rail.css",
)


def find_registered_agent_ids() -> set[str]:
    """Off the register() calls themselves, so a new agent is covered without editing this."""
    ids = set()
    for path in sorted(_AGENTS.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or getattr(node.func, "id", None) != "register":
                continue
            first = node.args[0] if node.args else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                ids.add(first.value)
    return ids


def find_agent_ids_named_in(text: str, agent_ids: set[str]) -> list[str]:
    """The id used as a VALUE, not the word: "editing" is also ordinary English."""
    return [
        # Quote- or slash-delimited: a string literal, or a URL path segment.
        agent_id for agent_id in sorted(agent_ids)
        if re.search(rf"""(?<=["'`/]){re.escape(agent_id)}(?=["'`/])""", text)
    ]


def test_registered_agent_ids_are_found() -> None:
    ids = find_registered_agent_ids()
    assert ids, f"no register(<id>, …) call found under {_AGENTS} — this rule is vacuous"


def test_the_predicate_catches_an_agent_id_used_as_a_value() -> None:
    ids = find_registered_agent_ids()
    sample = sorted(ids)[0]
    assert find_agent_ids_named_in(f'if agent_id == "{sample}":', ids) == [sample]
    assert not find_agent_ids_named_in(f"the reader is {sample} the workflow", ids)


def test_no_chat_host_names_an_agent() -> None:
    agent_ids = find_registered_agent_ids()
    offenders = {
        relative: named
        for relative in _GOVERNED
        if (named := find_agent_ids_named_in(
            (_APP / relative).read_text(encoding="utf-8"), agent_ids))
    }
    assert not offenders, (
        f"{offenders} name an agent. The rail and the panel hold a session id and render "
        "what the store gives them; branching on which agent is on the other end puts one "
        "agent's behaviour in the shell every agent shares. Whatever the surface needs to "
        "know, the agent should say: app.core.agent.registry.AgentConfig is where "
        "display_name lives, and read_display_name is how a router asks for it."
    )


def test_every_governed_file_exists() -> None:
    missing = [relative for relative in _GOVERNED if not (_APP / relative).is_file()]
    assert not missing, (
        f"{missing} are listed here but not in app/ — a renamed chat host silently drops "
        "out of this rule, so the list has to be corrected rather than left to pass."
    )

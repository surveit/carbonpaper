"""What the tour opens with. The greeting is written, stored as the agent's first turn,
and fed back into the system prompt — so the reader reads it and the model knows it said
it, and the script picks up from the question it ends on.
"""
from __future__ import annotations

import app.agents.tutorial.config  # noqa: F401 — registers the "tutorial" agent
from app.agents.tutorial.config import CONFIG as TUTORIAL_CONFIG
from app.core.agent.session import build_session_engine, create_agent_session
from app.core.agent.store import open_session_store
from app.web.breadcrumbs import _HOME_LABEL

_store = open_session_store()

_BASE_URL = "http://127.0.0.1:8788/"

# The four moves the greeting makes, in the order it must make them.
_MOVES = (
    f"Welcome to {_HOME_LABEL}.",
    "welcome to the tutorial",
    "seed a sample investigation",
    "Ready to get started?",
)


def _open_the_tour() -> str:
    return create_agent_session(
        "tutorial", {"base_url": _BASE_URL}, base_url=_BASE_URL, title="Guided tour"
    )


def _stored_greeting(sid: str) -> str:
    messages = _store.load(sid)["messages"]
    assert len(messages) == 1, messages
    assert messages[0]["role"] == "assistant"
    text: str = messages[0]["parts"][0]["text"]
    return text


def test_the_tour_stores_its_greeting_as_its_one_opening_turn() -> None:
    greeting = _stored_greeting(_open_the_tour())

    positions = [greeting.find(move) for move in _MOVES]
    assert -1 not in positions, [m for m, at in zip(_MOVES, positions) if at < 0]
    assert positions == sorted(positions), _MOVES


def test_the_greeting_says_what_the_product_is_for() -> None:
    """The three claims the tour then shows: prose in, typed stages out, every row traceable."""
    greeting = _stored_greeting(_open_the_tour())

    assert "You write your methodology as prose" in greeting
    assert "workflow of named, typed stages" in greeting
    assert "every row of the result traces back to the row it came from" in greeting


def test_the_greeting_closes_on_the_question_and_nothing_after_it() -> None:
    """Its two failure modes: a second place to go, and a gloss on why traceability matters."""
    greeting = _stored_greeting(_open_the_tour())

    assert greeting.endswith("Ready to get started?")
    assert "?" not in greeting[: -len("Ready to get started?")]


def test_the_model_is_told_the_greeting_it_opened_with() -> None:
    """Without this the reader's "yes" answers a question the model never saw."""
    sid = _open_the_tour()

    engine = build_session_engine(sid, _BASE_URL)

    assert _stored_greeting(sid) in engine._system_prompt


def test_the_hook_the_tutorial_agent_registered_is_the_one_that_runs() -> None:
    # Without this, every test above would pass on an agent carrying no hook.
    assert TUTORIAL_CONFIG.render_opening_message is not None

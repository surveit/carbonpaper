"""Replies the reader clicks instead of typing: the agent's own words, drawn as buttons."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agents.tutorial.prompt import TUTORIAL_OPENING_OFFERS
from app.core.agent.sdk_engine import _read_offered_steps
from app.core.agent.store import (
    OFFER_NEXT_STEPS,
    NextSteps,
    OffersBlock,
    _blocks_in_turn_order,
)
from app.main import app

client = TestClient(app)

_OPTIONS = ["Open the review queue", "Show me the workflow"]


def test_an_offer_part_renders_as_its_own_block_not_a_tool_row() -> None:
    blocks = _blocks_in_turn_order(
        {"role": "assistant", "parts": [
            {"type": "text", "text": "It stopped for a person."},
            {"type": "offer", "options": _OPTIONS},
        ]}
    )

    assert blocks[-1] == OffersBlock(options=_OPTIONS)


def test_the_engine_turns_the_call_into_an_offer_rather_than_a_tool_call() -> None:
    assert _read_offered_steps(OFFER_NEXT_STEPS, {"options": _OPTIONS}) == NextSteps(
        options=_OPTIONS)


def test_any_other_tool_is_left_as_the_tool_call_it_is() -> None:
    assert _read_offered_steps("run_workflow", {"options": _OPTIONS}) is None


def test_arguments_that_are_not_offerable_draw_as_an_ordinary_tool_row() -> None:
    """A model that called it wrongly gets the tool's own error back, and the reader sees the call."""
    assert _read_offered_steps(OFFER_NEXT_STEPS, {"options": ["only one"]}) is None


@pytest.mark.parametrize("options", [
    ["just the one"],
    ["a", "b", "c", "d", "e"],
    ["fine", "x" * 71],
])
def test_an_option_nobody_could_read_off_a_button_is_refused(options: list[str]) -> None:
    with pytest.raises(ValidationError):
        NextSteps(options=options)


def test_the_tour_opens_on_buttons_rather_than_a_blank_box() -> None:
    """The first screen is where a reader has least idea what to type."""
    page = client.get("/chat/agent/tutorial/new").text

    for option in TUTORIAL_OPENING_OFFERS:
        assert f'class="ac-offer">{option}</button>' in page

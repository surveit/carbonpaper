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
from app.tools.tutorial import offer_next_steps

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
    """An AI model that called it wrongly gets the error back, and the reader sees the call."""
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


def test_a_turn_that_offered_twice_shows_only_what_it_offered_last() -> None:
    """Seen in the tour: after the first call it wrote a second summary and offered again."""
    superseded = ["Open the queue", "Read the workflow"]

    blocks = _blocks_in_turn_order(
        {"role": "assistant", "parts": [
            {"type": "text", "text": "It stopped for a person."},
            {"type": "offer", "options": superseded},
            {"type": "text", "text": "Where it stands, again."},
            {"type": "offer", "options": _OPTIONS},
        ]}
    )

    assert [b for b in blocks if isinstance(b, OffersBlock)] == [OffersBlock(options=_OPTIONS)]


def test_what_comes_back_names_the_words_shown_and_ends_the_turn() -> None:
    result = offer_next_steps(_OPTIONS)

    for option in _OPTIONS:
        assert option in result
    assert "ends here" in result

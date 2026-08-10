"""The tour's script, as prose the model obeys: it talks before it acts, it never asks
permission to run, and every tool it names is one the tour actually holds."""
from __future__ import annotations

import re

from app.agents.tutorial.prompt import TUTORIAL_SYSTEM_PROMPT
from app.tools.tool_specs import TOOL_SPECS
from app.tools.tutorial import TutorialContext, make_tutorial_tools

_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")


def _tour_tool_names() -> set[str]:
    return {
        spec.name
        for spec in make_tutorial_tools(TutorialContext(base_url="http://x/"))
    }


def test_the_prompt_names_no_tool_the_tour_does_not_hold() -> None:
    known = set(TOOL_SPECS) | _tour_tool_names()
    # A retired tool still named in the script reads to the model as an instruction.
    named = set(_IDENTIFIER.findall(TUTORIAL_SYSTEM_PROMPT)) & known

    assert named <= _tour_tool_names(), sorted(named - _tour_tool_names())
    assert {"create_tutorial_project", "run_workflow", "wait_for_run"} <= named


def test_beat_one_is_conversation_and_calls_no_tool() -> None:
    beat = _beat(1)

    assert "No tools in this message" in beat
    assert "STOP" in beat and "let them answer" in beat
    assert "Beat 1 calls no tool." in TUTORIAL_SYSTEM_PROMPT


def test_the_workflow_is_introduced_by_why_it_exists_not_by_its_stage_list() -> None:
    beat = _beat(2)

    assert "ONE sentence" in beat and "what this workflow is FOR" in beat
    assert "Do NOT list the five stages in the chat." in beat
    assert "workflow_url" in beat and "guide_url" in beat


def test_the_tour_never_asks_permission_to_run() -> None:
    assert "Do not ask whether to run it" in _beat(3)
    assert "Again without asking." in _beat(5)
    assert "Never ask permission to run the workflow." in TUTORIAL_SYSTEM_PROMPT


def test_the_tour_waits_in_one_call_and_never_abandons_a_run() -> None:
    beat = _beat(3)

    assert "call wait_for_run ONCE and let\n   it block" in beat
    assert "call wait_for_run again" in beat
    assert "Never abandon a run you\n   started" in beat
    assert "is NOT a\n  failure" in TUTORIAL_SYSTEM_PROMPT


def test_the_walk_beat_sends_the_reader_to_the_stored_review_guide() -> None:
    assert "The guide rail on `run_url`" in _beat(4)


def test_the_no_fabrication_rules_survive_the_rewrite() -> None:
    for rule in (
        "The sample data is SYNTHETIC",
        "Never state a number, row count, duration, version or finding you did not read",
        "Never claim a capability this tour did not demonstrate",
        "If a tool has not told you a number, you do\nnot have it.",
    ):
        assert rule in TUTORIAL_SYSTEM_PROMPT


def _beat(number: int) -> str:
    """The numbered beat's own text, so a rule asserted here is one THAT beat carries."""
    start = TUTORIAL_SYSTEM_PROMPT.index(f"\n{number}. ")
    rest = TUTORIAL_SYSTEM_PROMPT[start + 1 :]
    end = rest.find(f"\n{number + 1}. ")
    return rest if end < 0 else rest[:end]

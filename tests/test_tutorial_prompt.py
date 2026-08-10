"""The tour's script, as prose the model obeys: it talks before it acts, it seeds and runs
in one turn with no boundary to ask permission at, and every tool and button it names is
one that exists."""
from __future__ import annotations

import re
from pathlib import Path

import app
from app.agents.tutorial.prompt import TUTORIAL_SYSTEM_PROMPT
from app.tools.tool_specs import TOOL_SPECS
from app.tools.tutorial import TutorialContext, make_tutorial_tools

_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")
_TEMPLATES = Path(app.__file__).resolve().parent / "templates"

# Labels beat 4 sends the reader to click. Each must be a string the app renders.
_NAMED_CONTROLS = ("View lineage", "Export review packet", "Generate examples")


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
    beat = _flat(_beat(1))

    assert "No tools in this message" in beat
    assert "STOP" in beat and "let them answer" in beat
    assert "Beat 1 calls no tool." in TUTORIAL_SYSTEM_PROMPT


def test_the_workflow_is_introduced_by_why_it_exists_not_by_its_stage_list() -> None:
    beat = _flat(_beat(2))

    assert "ONE sentence" in beat and "what this EXAMPLE workflow is for" in beat
    assert "The filter is not the point; the LEAD is" in beat
    assert "what the client said in public against what the same client paid to ask" in beat
    assert "Do NOT list the seven stages in the chat" in beat
    assert "workflow_url" in beat and "guide_url" in beat


def test_the_worked_beat_calls_it_an_example_workflow_and_says_what_it_is_hunting() -> None:
    """Named as an example, and carrying the say-versus-do lead rather than the filter."""
    worked = _flat(TUTORIAL_SYSTEM_PROMPT)

    assert "This example workflow puts what an organization promised in public" in worked
    assert "flags the filings asking for the opposite of the promise" in worked
    assert "earns a reporter's phone call" in worked


def test_the_tour_admits_the_dataset_is_deliberately_engineered() -> None:
    """Not "synthetic" in passing: what was engineered, and what that costs."""
    beat = _flat(_beat(2))

    assert "ADMIT WHAT THIS DATASET IS" in beat
    assert 'Not "synthetic" and on to the next sentence' in beat
    assert "invented AND DELIBERATELY ENGINEERED so the contradiction is obvious" in beat
    for cost in ("real filings run to pages", "the names never line up",
                 "much harder than what they are about to watch"):
        assert cost in beat, cost
    assert "a property of the demo, stated as such" in beat
    assert "not a disclaimer to hurry past" in beat


def test_the_engineered_data_admission_is_a_hard_rule_too() -> None:
    rules = _flat(TUTORIAL_SYSTEM_PROMPT)

    assert "INVENTED AND DELIBERATELY ENGINEERED so the contradiction is obvious" in rules
    assert '"Synthetic" on its own does not discharge this rule' in rules


def test_the_worked_beat_shows_the_admission_as_its_own_paragraph() -> None:
    worked = _flat(TUTORIAL_SYSTEM_PROMPT)

    assert "it is deliberately engineered to make that contradiction obvious" in worked
    assert "the shape of the analysis, not the difficulty of it" in worked
    assert "The admission about the data is a paragraph of its own" in worked


def test_seeding_and_running_are_one_turn_with_no_boundary_to_ask_at() -> None:
    """One message, so no turn end arrives where a permission question would fit."""
    beat = _flat(_beat(2))

    assert "ALL IN ONE TURN" in beat
    assert "One message, three tool calls, no pause anywhere inside it" in beat
    for tool in ("create_tutorial_project", "run_workflow", "wait_for_run"):
        assert tool in beat
    assert "Do not end your turn between them" in beat
    assert (
        "Beat 2 is ONE turn. create_tutorial_project, run_workflow and wait_for_run "
        "happen with no message between them"
    ) in _flat(TUTORIAL_SYSTEM_PROMPT)


def test_the_seed_and_run_beat_puts_no_question_to_the_reader() -> None:
    """A question mark here is the regression: this beat was where it paused to ask."""
    beat = _beat(2)

    assert "?" not in beat, beat
    assert not re.search(
        r"(shall|should|may|can|want me to|would you like me to)\b[^.]{0,60}\brun\b",
        beat,
        re.IGNORECASE,
    ), beat


def test_the_tour_never_asks_permission_to_run() -> None:
    assert "do not ask whether to run it" in _flat(_beat(2))
    assert "Again without asking" in _flat(_beat(4))
    assert "Never ask permission to run the workflow." in TUTORIAL_SYSTEM_PROMPT


def test_the_tour_waits_in_one_call_and_never_abandons_a_run() -> None:
    beat = _flat(_beat(2))

    assert "Then wait_for_run ONCE, and let it block" in beat
    assert "call wait_for_run again" in beat
    assert "Never abandon a run you started" in beat
    assert "is NOT a failure" in _flat(TUTORIAL_SYSTEM_PROMPT)


def test_the_run_beat_ends_by_handing_over_rather_than_offering_a_menu() -> None:
    beat = _flat(_beat(2))

    assert "go click that link, poke around it, and come back when they are done" in beat
    assert "No menu" in beat and "no question" in beat
    assert "The page is the thing now, not you." in beat


def test_the_return_beat_offers_exploring_or_starting_their_own_workflow() -> None:
    beat = _flat(_beat(3))

    assert "WHEN THEY COME BACK, OFFER A REAL CHOICE" in beat
    assert "keep looking around what is already here" in beat
    assert "start on a workflow of their own" in beat
    assert "Ask which they want." in beat


def test_every_control_the_tour_sends_them_to_click_is_one_the_app_renders() -> None:
    """A button named here that does not exist sends the reader looking for nothing."""
    rendered = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(_TEMPLATES.glob("*.html"))
    )
    beat = _flat(_beat(4))

    for label in _NAMED_CONTROLS:
        assert label in beat, label
        assert label in rendered, f"{label} is named in the tour but rendered nowhere"


def test_the_tour_does_not_promise_an_in_app_button_for_agent_editing() -> None:
    """There is none (tests/test_web_smoke.py pins that), so the tour points at MCP."""
    beat = _flat(_beat(4))

    assert "There is no button for this in the app, and you must not invent one" in beat
    assert "MCP client connected to this workspace" in beat
    assert "Edit with agent" not in TUTORIAL_SYSTEM_PROMPT


def test_the_tour_starts_lineage_from_a_data_stage_not_from_the_report() -> None:
    """Lineage stops at the publish stage, so from the report the walk is one step."""
    beat = _flat(_beat(4))

    assert "NOT from the report" in beat
    assert "lineage stops at the publish stage" in beat
    assert "significant_filings" in beat and "flag_contradiction" in beat


def test_the_tour_points_at_the_unmatched_rows_absent_parent() -> None:
    """A null commitment could be an unmatched row or a null cell; the parent tells them apart."""
    beat = _flat(_beat(4))

    assert "matched_commitments` is a left join" in beat
    assert "ONE parent where a matched filing shows two" in beat
    assert "The absent second parent IS the non-match record" in beat


def test_the_no_fabrication_rules_survive_the_rewrite() -> None:
    for rule in (
        "The sample data is INVENTED AND DELIBERATELY ENGINEERED",
        "Never state a number, row count, duration, version or finding you did not read",
        "Never claim a capability this tour did not demonstrate",
        "If a tool has not told you a number, you do not have it.",
        "Never name a button you have not been told exists.",
    ):
        assert rule in _flat(TUTORIAL_SYSTEM_PROMPT)


def _beat(number: int) -> str:
    """The numbered beat's own text, so a rule asserted here is one THAT beat carries."""
    start = TUTORIAL_SYSTEM_PROMPT.index(f"\n{number}. ")
    rest = TUTORIAL_SYSTEM_PROMPT[start + 1 :]
    end = rest.find(f"\n{number + 1}. ")
    return rest if end < 0 else rest[:end]


def _flat(text: str) -> str:
    # Assertions read the words, not where the paragraph happened to wrap.
    return " ".join(text.split())

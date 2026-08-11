"""The tour's script, as prose the model obeys: it talks before it acts, it seeds and runs
in one turn with no boundary to ask permission at, and every tool and button it names is
one that exists."""
from __future__ import annotations

import re
from pathlib import Path

import app
from app.agents.tutorial.prompt import TUTORIAL_SYSTEM_PROMPT
from app.tools.tool_specs import TOOL_SPECS
from app.agents.tutorial.config import make_tutorial_tools
from app.tools.tutorial import TutorialContext

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
    assert {"create_tutorial_project", "run_workflow", "get_run_status"} <= named


def test_beat_one_is_conversation_and_calls_no_tool() -> None:
    beat = _flat(_beat(1))

    assert "No tools in this message" in beat
    assert "STOP" in beat and "let them answer" in beat
    assert "Beat 1 calls no tool." in TUTORIAL_SYSTEM_PROMPT


def test_the_greeting_welcomes_them_twice_then_says_only_what_it_will_do() -> None:
    """To the tool, then to the tutorial, then one line on the seeding — and stop."""
    beat = _flat(_beat(1))

    assert "WELCOME THEM TO CARBONPAPER" in beat
    assert "WELCOME THEM TO THE TUTORIAL" in beat
    assert "seed a sample investigation for them to explore" in beat
    assert "That is the whole of it" in beat
    assert "Do not also enumerate running it, handing over a record" in beat


def test_the_greeting_is_prompted_by_a_hello_not_by_an_instruction() -> None:
    """An instruction gets performed; a hello gets answered. The tour wants the answer."""
    from app.agents.tutorial.prompt import TUTORIAL_OPENING_PROMPT

    assert TUTORIAL_OPENING_PROMPT == "Hi"


def test_the_tour_writes_the_product_name_in_lower_case() -> None:
    """Three casings exist in this repo; prose and CLAUDE.md use the lower-case one."""
    assert 'The product is written "carbonpaper", lower case' in _flat(_beat(1))
    assert "Carbonpaper" not in TUTORIAL_SYSTEM_PROMPT
    assert "CarbonPaper" not in TUTORIAL_SYSTEM_PROMPT


def test_the_greeting_asks_only_whether_they_are_ready() -> None:
    """They clicked into the tutorial; offering somewhere else is a stall, not a choice."""
    beat = _flat(_beat(1))

    assert "asking whether they are ready to get started" in beat
    assert "Offering them somewhere else to go" in beat
    assert "a choice with one real option is a stall" in beat


def test_the_greeting_says_the_agent_writes_the_stages_not_the_reader() -> None:
    """The reader authors prose; the stage graph is compiled from it by an agent."""
    beat = _flat(_beat(1))

    assert "they write their methodology" in beat and "an AI agent turns it into" in beat
    assert (
        "The reader writes their methodology as prose and an AI agent turns it into "
        "a workflow of named, typed stages — they do not write the stages themselves."
    ) in _flat(TUTORIAL_SYSTEM_PROMPT)


def test_the_greeting_carries_no_closing_gloss_on_why_traceability_matters() -> None:
    """The run about to happen is the argument; making it in advance is chat behaviour."""
    beat = _flat(_beat(1))

    assert "A closing gloss on why traceability matters" in beat
    assert "so you can look at real rows, not a description of them" in beat


def test_the_workflow_is_introduced_by_why_it_exists_not_by_its_stage_list() -> None:
    beat = _flat(_beat(2))

    assert "ONE sentence" in beat and "what this EXAMPLE workflow is for" in beat
    assert "The mechanics are not the point; the LEAD is" in beat
    assert "what the client said in public against what the same client paid to ask" in beat
    assert "Do NOT list the six stages in the chat" in beat


def test_the_worked_beat_calls_it_an_example_workflow_and_says_what_it_is_hunting() -> None:
    """Named as an example, and carrying the say-versus-do lead rather than the filter."""
    worked = _flat(TUTORIAL_SYSTEM_PROMPT)

    assert "This example workflow puts what an organization promised in public" in worked
    assert "flags the filings asking for the opposite of the promise" in worked


def test_the_tour_admits_the_dataset_is_invented_in_a_sentence_of_its_own() -> None:
    """One line, before any claim about the data — not a paragraph on how it was built."""
    beat = _flat(_beat(2))

    assert "before anything else about the data, say plainly that it is invented" in beat
    assert "One sentence of its own" in beat
    assert "not a paragraph on how the demo was built" in beat


def test_the_invented_data_admission_is_a_hard_rule_too() -> None:
    rules = _flat(TUTORIAL_SYSTEM_PROMPT)

    assert "The sample data is INVENTED, and you say so plainly at beat 2" in rules
    assert '"Synthetic"' in rules and "does not discharge this rule" in rules


def test_the_worked_beat_shows_the_admission_as_its_own_line() -> None:
    worked = _flat(TUTORIAL_SYSTEM_PROMPT)

    assert "The data is invented." in worked
    assert "The data is admitted to be invented, in a line of its own" in worked


def test_the_run_beat_hands_over_exactly_one_link() -> None:
    """Three links at the end of a turn is three decisions; the run is the one to make."""
    beat = _flat(_beat(2))

    assert "That link is the ONLY one this beat hands over" in beat
    assert "Beat 2 ends on ONE link, the run's." in _flat(TUTORIAL_SYSTEM_PROMPT)
    # The one URL the tour joins, and only from two things a tool returned.
    assert "`runs_url_prefix` with that `run_id` on the end" in beat
    # The other two pages are not lost — beat 4 is where they are offered.
    assert "the first beat that may hand over `workflow_url`" in _flat(_beat(4))
    assert "guide_url" in _flat(_beat(4))


def test_seeding_and_running_are_one_turn_with_no_boundary_to_ask_at() -> None:
    """One message, so no turn end arrives where a permission question would fit."""
    beat = _flat(_beat(2))

    assert "ALL IN ONE TURN" in beat
    assert "One message, three tool calls, no pause anywhere inside it" in beat
    for tool in ("create_tutorial_project", "run_workflow", "get_run_status"):
        assert tool in beat
    assert "Do not end your turn between them" in beat
    assert (
        "Beat 2 is ONE turn. create_tutorial_project, run_workflow and the "
        "get_run_status polling happen with no message between them"
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


def test_the_tour_polls_the_run_out_and_never_abandons_it() -> None:
    beat = _flat(_beat(2))

    assert "poll get_run_status until its `status` is no longer `running`" in beat
    assert "it is a run still going, and you call again" in beat
    assert "Never abandon a run you started" in beat
    assert "is NOT a failure" in _flat(TUTORIAL_SYSTEM_PROMPT)


def test_the_run_beat_ends_by_handing_over_rather_than_offering_a_menu() -> None:
    beat = _flat(_beat(2))

    assert "asking them to explore the run and come back when they are done" in beat
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
    assert "matched_commitments" in beat and "flag_contradiction" in beat


def test_the_tour_points_at_the_unmatched_rows_absent_parent() -> None:
    """A null commitment could be an unmatched row or a null cell; the parent tells them apart."""
    beat = _flat(_beat(4))

    assert "matched_commitments` is a left join" in beat
    assert "ONE parent where a matched filing shows two" in beat
    assert "The absent second parent IS the non-match record" in beat


def test_the_no_fabrication_rules_survive_the_rewrite() -> None:
    for rule in (
        "The sample data is INVENTED, and you say so plainly at beat 2",
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

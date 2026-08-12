"""The tour's script, as prose the model obeys: it talks before it acts, it seeds and runs
in one turn with no boundary to ask permission at, and every tool and button it names is
one that exists."""
from __future__ import annotations

import re
from pathlib import Path

import app
from app.agents.tutorial.prompt import TUTORIAL_SYSTEM_PROMPT
from app.core.run_status import RunStatus
from app.models.run_manifest import QueueStats, RunManifest
from app.web.breadcrumbs import _HOME_LABEL
from app.tools.tool_specs import TOOL_SPECS
from app.agents.tutorial.config import make_tutorial_tools
from app.services.project import WorkflowFile
from app.models import StageType
from app.services.workspace import StageSummary
from app.tools.shared import StageOutputRow, StageOutputRows
from app.tools.tutorial import _FIXTURE, TutorialContext, TutorialProject

_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")
_TEMPLATES = Path(app.__file__).resolve().parent / "templates"

# Labels beat 4 sends the reader to click. Each must be a string the app renders.
_NAMED_CONTROLS = (
    "View lineage",
    "Export review packet",
    "Example behavior",
    "Generate examples",
    "Edit with agent",
)


def _tour_tool_names() -> set[str]:
    return {
        spec.name
        for spec in make_tutorial_tools(TutorialContext(base_url="http://x/"))
    }


def _tour_tool_arguments() -> set[str]:
    return {
        argument
        for spec in make_tutorial_tools(TutorialContext(base_url="http://x/"))
        for argument in spec.input_schema
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


def test_the_greeting_is_prompted_by_a_hello_not_by_an_instruction() -> None:
    """An instruction gets performed; a hello gets answered. The tour wants the answer."""
    from app.agents.tutorial.prompt import TUTORIAL_OPENING_PROMPT

    assert TUTORIAL_OPENING_PROMPT == "Hi"


def test_the_tour_writes_the_product_name_the_page_around_it_writes() -> None:
    """Read off the breadcrumb: the header names the product while the tour is speaking."""
    assert f'The product is written "{_HOME_LABEL}"' in _flat(_beat(1))
    for wrong in ("carbonpaper", "Carbonpaper", "CarbonPaper"):
        assert wrong not in TUTORIAL_SYSTEM_PROMPT


def test_the_greeting_says_the_agent_writes_the_stages_not_the_reader() -> None:
    """The reader authors prose; the stage graph is compiled from it by an agent."""
    beat = _flat(_beat(1))

    assert "they write their methodology" in beat and "an AI agent turns it into" in beat
    assert (
        "The reader writes their methodology as prose and an AI agent turns it into "
        "a workflow of named, typed stages — they do not write the stages themselves."
    ) in _flat(TUTORIAL_SYSTEM_PROMPT)


def test_the_workflow_is_introduced_by_why_it_exists_not_by_its_stage_list() -> None:
    beat = _flat(_beat(2))

    assert "ONE sentence" in beat and "what this EXAMPLE workflow is for" in beat
    assert "what a company committed to in public against what the same company" in beat
    assert "Do NOT list the stages in the chat" in beat
    assert "Nor the files it reads" in beat


def test_the_invented_data_admission_is_a_hard_rule_too() -> None:
    rules = _flat(TUTORIAL_SYSTEM_PROMPT)

    assert "The sample data is INVENTED, and you say so plainly at beat 2" in rules
    assert '"Synthetic"' in rules and "does not discharge this rule" in rules


def test_the_run_beat_hands_over_exactly_one_link() -> None:
    """Three links at the end of a turn is three decisions; the run is the one to make."""
    beat = _flat(_beat(2))

    # Stated once, in the hard rules — the beat used to repeat the argument.
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
    assert "One message, no pause anywhere inside it" in beat
    for tool in ("create_tutorial_project", "run_workflow", "sleep", "get_run_status"):
        assert tool in beat
    assert "Do not end your turn between them" in beat
    assert (
        "Beat 2 is ONE turn. create_tutorial_project, run_workflow, sleep and "
        "get_run_status happen with no message between them"
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


def test_the_run_beat_ends_by_handing_over_rather_than_offering_a_menu() -> None:
    beat = _flat(_beat(2))

    assert "sending them to the run and offering to answer questions" in beat
    assert "No menu" in beat and "no question" in beat
    assert "The page is the thing now, not you." in beat


def test_every_control_the_tour_sends_them_to_click_is_one_the_app_renders() -> None:
    """A button named here that does not exist sends the reader looking for nothing."""
    rendered = _rendered_templates()
    beat = _flat(_beat(4))

    for label in _NAMED_CONTROLS:
        assert label in beat, label
        assert label in rendered, f"{label} is named in the tour but rendered nowhere"


def test_the_run_stopping_for_a_reviewer_is_a_beat_of_its_own() -> None:
    """awaiting_review is the workflow working; a tour that reads it as an error stops there."""
    beat = _flat(_beat(3))

    assert "`awaiting_review` is the expected ending" in _flat(_beat(2))
    assert "queue" in beat


def test_the_tour_says_plainly_that_the_review_is_not_its_to_do() -> None:
    beat = _flat(_beat(3))

    assert "cannot decide a card or resume the run" in beat


def test_the_queue_link_is_joined_only_from_what_a_tool_returned() -> None:
    """The second URL the tour builds; a remembered path here is an invented page."""
    beat = _flat(_beat(3))

    assert "the run's page, then `/queue/`, then the queue stage's id" in beat
    assert "whose `type` is `human_review_queue`" in beat


def _rendered_templates() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(_TEMPLATES.glob("*.html"))
    )


def test_the_script_walks_five_beats() -> None:
    """The run halting for a reviewer is a beat of its own, between the run and the list."""
    numbered = [int(n) for n in re.findall(r"\n(\d+)\. [A-Z]", TUTORIAL_SYSTEM_PROMPT)]

    assert numbered == [1, 2, 3, 4, 5]
    assert "Walk these five beats in order." in TUTORIAL_SYSTEM_PROMPT


def test_the_beat_after_the_run_hands_the_list_over_rather_than_offering_to() -> None:
    """The round trip this fixes: a reader told to look around had to ask again for the list."""
    beat = _flat(_beat(4))

    assert "Not two doors and a question" in beat
    assert "these, and only these, a line each" in beat
    # Their own workflow is one line at the end of the list, not a door competing with it.
    assert "Close on their own workflow, one line" in beat


def test_editing_is_reached_by_a_link_and_the_mcp_route_needs_no_terminal() -> None:
    """A reader told to open a terminal reads this product as their developer's."""
    beat = _flat(_beat(5))

    # The in-app route leads, and it is a link a tool returned.
    assert "HAND OVER `edit_chat_url`" in beat
    assert beat.index("edit_chat_url") < beat.index("mcp_command")
    assert "ask that assistant to add this workspace as an MCP server" in beat
    assert "not a terminal they have to open" in beat
    # The in-app control is still named where the reader is shown around.
    assert '"Edit with agent"' in _flat(_beat(4))
    assert "There is no button for this in the app" not in _flat(TUTORIAL_SYSTEM_PROMPT)


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



# Every name the script quotes in backticks is something the code defines: a stage of the
# fixture, a column of one of its schemas, a field of what a tour tool returns, a stage
# type, a tool, an argument one of those tools takes, or a run status. A renamed stage, column, field or
# argument otherwise leaves the prompt pointing at nothing, silently.
_RUN_STATUS_WORDS = {status.value for status in RunStatus} | {"status", "error"}
# Owed by the branch adding the tour's editing-chat tool: the field its payload
# carries. DELETE this set when that tool lands and TutorialProject declares it.
_PENDING_TOUR_TOOL_FIELDS = {"edit_chat_url"}


def test_every_name_the_prompt_quotes_is_one_the_code_defines() -> None:
    fixture = WorkflowFile.model_validate_json(_FIXTURE.read_text(encoding="utf-8"))
    known = (
        {stage.id for stage in fixture.stages}
        | _fixture_column_names(fixture)
        | set(TutorialProject.model_fields)
        | set(StageOutputRow.model_fields)
        | set(StageOutputRows.model_fields)
        # The script names stages by what `workflow` calls them: a field, and a type.
        | set(StageSummary.model_fields)
        | {stage_type.value for stage_type in StageType}
        | _tour_tool_names()
        | _tour_tool_arguments()
        | set(RunManifest.model_fields)
        | set(QueueStats.__annotations__)
        | _RUN_STATUS_WORDS
        | _PENDING_TOUR_TOOL_FIELDS
    )
    quoted = set(re.findall(r"`([a-z][a-z0-9_]{3,})`", TUTORIAL_SYSTEM_PROMPT))

    assert quoted <= known, sorted(quoted - known)


def _fixture_column_names(fixture: WorkflowFile) -> set[str]:
    """Off the signatures, which is where a stage's columns are declared."""
    signatures = [stage.signature for stage in fixture.stages]
    return {
        column.name
        for signature in signatures
        for column in [
            *(c for entry in signature.reads for c in entry.columns),
            *getattr(signature, "adds", []),
            *getattr(signature, "rewrites", []),
            *getattr(signature, "produces", []),
        ]
    }

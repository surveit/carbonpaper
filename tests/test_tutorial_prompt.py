"""The tour's script, as prose the model obeys: it talks before it acts, it seeds and runs
in one turn with no boundary to ask permission at, and every tool and button it names is
one that exists."""
from __future__ import annotations

import re
from pathlib import Path

import app
from app.agents.tutorial.prompt import TUTORIAL_SYSTEM_PROMPT, _WORKED_BEAT
from app.core.run_status import RunStatus
from app.models.run_manifest import QueueStats
from app.runtime.manifest import RunManifest
from app.web.breadcrumbs import _HOME_LABEL
from app.tools.tool_specs import find_tool_names
from app.agents.tutorial.config import make_tutorial_tools
from app.services.project import Project, WorkflowFile
from app.models import StageType
from app.services.workspace import StageSummary
from app.tools.shared import StageOutputRow, StageOutputRows
from app.tools.tutorial import _FIXTURE, TutorialAgentReference, TutorialContext

_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")
_TEMPLATES = Path(app.__file__).resolve().parent / "templates"

# Labels the tour sends the reader to click. Each must be a string the app renders.
_NAMED_CONTROLS = (
    "Resume run",
    "View lineage",
    "Export review packet",
    "Example behavior",
    "Generate examples",
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
        for argument in spec.json_schema.get("properties", {})
    }


def test_the_prompt_names_no_tool_the_tour_does_not_hold() -> None:
    known = find_tool_names() | _tour_tool_names()
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


def test_the_run_beat_hands_over_exactly_one_link_and_it_is_the_queues() -> None:
    """The turn's action is deciding the flagged filings, so it ends where that is done."""
    beat = _flat(_beat(2))

    # Stated once, in the hard rules — the beat used to repeat the argument.
    assert "Beat 2 ends on ONE link, the queue's." in _flat(TUTORIAL_SYSTEM_PROMPT)
    assert "ONE LINK, AND IT IS THE QUEUE'S" in beat
    assert "the run's page is not offered in this turn" in beat
    # Both URLs it joins come from what a tool returned, and nothing else.
    assert "`runs_url_prefix` with that `run_id` on the end" in beat
    # The run's own page and the two others are not lost — beat 3 is where they land.
    assert "the first beat that may hand over the run's own page, `workflow_url`" in _flat(
        _beat(3))
    assert "guide_url" in _flat(_beat(3))


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

    assert "No menu" in beat and "no question" in beat
    # The live tour signed off with this line, copied straight from the worked example.
    assert 'never "let me know if you have any questions"' in beat
    assert "The queue is the thing now, not you." in beat


def test_the_polling_between_a_sleep_and_a_check_is_banned_in_the_words_it_took() -> None:
    """A rule with no example lost to an example with no rule: the live run narrated twice."""
    beat = _flat(_beat(2))

    assert "WRITE NOTHING BETWEEN THOSE CALLS" in beat
    for line in ('"still running"', '"let me check again"', '"checking again"'):
        assert line in beat, line
    assert "The tool calls arriving ARE the progress indicator" in beat
    assert (
        'you sleep again and check again, WRITING NOTHING between the two calls'
    ) in _flat(TUTORIAL_SYSTEM_PROMPT)


def test_the_close_never_leads_with_the_manifest_status_token() -> None:
    """`awaiting_review` is a stored value; read cold at a reader it looks like an error code."""
    beat = _flat(_beat(2))

    assert "NEVER OPEN ON THE RAW STATUS" in beat
    assert "a reader meeting it first reads it as an error code" in beat
    assert "never lead with the status word itself" in _flat(TUTORIAL_SYSTEM_PROMPT).lower()


# What the merged close owes the reader, in the order it owes it. The beat states the
# rule and the worked example shows it, and they are read together — so both carry all
# five. The two used to be separate turns: one too terse to say why, one twice as long.
_CLOSING_CLAUSES = (
    "proposed a judgement on",
    "asking government for the opposite of what the client promised in public",
    "stopped there on purpose",
    "not published on a model's say-so",
    "are waiting for",
    "keep or change the model's label",
    "resume run",
    "publishes the report carrying",
)


def test_the_close_explains_the_pause_and_what_it_asks_of_them() -> None:
    for source in (_beat(2), _WORKED_BEAT):
        text = _flat(source).lower()
        positions = [text.find(clause) for clause in _CLOSING_CLAUSES]

        assert -1 not in positions, [
            clause for clause, at in zip(_CLOSING_CLAUSES, positions) if at < 0
        ]
        assert positions == sorted(positions), _CLOSING_CLAUSES


def test_the_one_count_the_close_may_state_is_read_off_the_manifest() -> None:
    """The verbose turn leaked `items_pending: 2 on the review_contradictions stage`."""
    beat = _flat(_beat(2))

    assert "ONE clause, because it says how much work" in beat
    assert "`items_pending`" in beat and "`human_review_queue_stats`" in beat
    assert 'Write it as "two filings are waiting for you"' in beat
    assert "never the field name, never the stage id" in beat


def test_the_close_asks_for_their_name_once_and_does_not_dwell_on_what_it_cannot_do() -> None:
    beat = _flat(_beat(2))

    assert "the name said ONCE in the whole turn, not in every sentence" in beat
    assert "One short clause saying the deciding is theirs and not yours is the most" in beat


def test_the_worked_beat_models_the_silence_and_the_close_it_asks_for() -> None:
    """The example is what the model copies, so both faulty closes are shown AS failures."""
    worked = _flat(_WORKED_BEAT)

    assert "and not one word written between them" in worked
    # The terse close is quoted so the model meets the exact lines it produced.
    assert "A close that fails" in worked
    assert "Still running — let me check again." in worked
    assert "Status: awaiting_review." in worked
    assert "the third hands the reader a manifest value that reads like an error code" in worked
    # …and so is the verbose one, which said the same thing at twice the length.
    assert "The other way this close fails is by saying all of it twice as long" in worked
    assert "`items_pending: 2 on the review_contradictions stage`" in worked
    # The passing example ends on the queue's link, with no filler under it.
    passing = worked[: worked.index("A close that fails")]
    assert "Let me know if you have any questions." not in passing
    assert "<runs_url_prefix><run_id>/queue/<queue stage id>" in passing


def test_the_announced_wait_is_the_one_duration_the_rules_exempt() -> None:
    """A duration stated before the run exists must be the script's own, not a guess."""
    beat = _flat(_beat(2))

    assert "it may take about a minute" in beat
    assert "since a real model reads the filings" in beat
    assert "about-a-minute expectation" in _flat(TUTORIAL_SYSTEM_PROMPT)


def test_the_run_beat_recites_none_of_the_data_the_pages_hold() -> None:
    beat = _flat(_beat(2))

    assert "no row counts, no per-stage account" in beat
    assert "no reciting numbers the pages already hold" in beat


def test_every_control_the_tour_sends_them_to_click_is_one_the_app_renders() -> None:
    """A button named here that does not exist sends the reader looking for nothing."""
    rendered = _rendered_templates()
    script = _flat(TUTORIAL_SYSTEM_PROMPT)

    for label in _NAMED_CONTROLS:
        assert label in script, label
        assert label in rendered, f"{label} is named in the tour but rendered nowhere"


def test_the_halt_is_explained_in_the_turn_that_hits_it_not_a_turn_later() -> None:
    """It used to wait for the reader to say "looks good" before saying what was waiting."""
    beat = _flat(_beat(2))

    assert "`awaiting_review` is the expected ending" in beat
    assert "WHEN IT SETTLES, IN THIS SAME TURN, SAY WHAT IS WAITING FOR THEM" in beat
    assert "do not stop on it and wait to be asked what it means" in beat
    # The explore beat is what the reader's reply reaches, and only once they resumed.
    assert "`ok` means they decided the cards and resumed it" in beat
    assert "go on to beat 3" in beat


def test_the_queue_link_is_joined_only_from_what_a_tool_returned() -> None:
    """The second URL the tour builds; a remembered path here is an invented page."""
    beat = _flat(_beat(2))

    assert "that page, then `/queue/`, then the queue stage's id" in beat
    assert "whose `type` is `human_review_queue`" in beat


def _rendered_templates() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(_TEMPLATES.glob("*.html"))
    )


def test_the_script_walks_four_beats() -> None:
    """The halt is no longer a beat of its own: it closes the turn that hit it."""
    numbered = [int(n) for n in re.findall(r"\n(\d+)\. [A-Z]", TUTORIAL_SYSTEM_PROMPT)]

    assert numbered == [1, 2, 3, 4]
    assert "Walk these four beats in order." in TUTORIAL_SYSTEM_PROMPT


def test_the_beat_after_the_run_hands_the_list_over_rather_than_offering_to() -> None:
    """The round trip this fixes: a reader told to look around had to ask again for the list."""
    beat = _flat(_beat(3))

    assert "Not two doors and a question" in beat
    assert "these, and only these, a line each" in beat


def test_the_explore_beat_ends_on_one_call_to_action_and_not_a_farewell() -> None:
    beat = _flat(_beat(3))

    assert "THEN CLOSE ON THE ONE THING TO DO NEXT: start a project of their own" in beat
    assert "no menu, and nothing about the tour's project being theirs too" in beat
    assert "Offer it the two ways beat 4 writes, in beat 4's words" in beat


def test_the_eval_is_the_fourth_thing_to_explore_and_names_no_figure_of_its_own() -> None:
    """`eval_url` is a tool's; the eval's size and score are the page's to state."""
    beat = _flat(_beat(3))

    assert "Hand over `eval_url`, which create_tutorial_project returned" in beat
    assert "carrying the label a person settled from the methodology BEFORE the workflow ran" in beat
    assert "It scores one column — the judgement itself — as an exact match" in beat
    assert "name no count and no accuracy from here" in beat
    # (e), the uncapped re-run, is gone: nothing in the tour spends on model calls now.
    assert "uncapped" not in _flat(TUTORIAL_SYSTEM_PROMPT).lower()
    assert "Edit with agent" not in beat


def test_the_call_to_action_leads_with_the_new_chat_here() -> None:
    """The reader asked to start their OWN; the tour used to send them to the tour's."""
    beat = _flat(_beat(4))

    assert "PRIMARY — HERE, IN A NEW CHAT: `new_project_chat_url`" in beat
    assert "This is THE call to action, and it leads" in beat
    assert "bound to NO project" in beat
    assert "it creates the project from what they wrote and writes the stages" in beat
    assert "Do not send them to any page of the tutorial project for this" in beat
    # The chat link leads; MCP is the advanced second; the tour's project is not a door.
    assert beat.index("new_project_chat_url") < beat.index("mcp_command")
    assert beat.index("mcp_command") < beat.index("edit_chat_url")
    assert "not a third door on the call to action" in beat


def test_the_mcp_route_is_the_advanced_second_and_says_where_the_tools_appear() -> None:
    beat = _flat(_beat(4))

    # Claude Code reads a newly added server at session start, so none of its tools show
    # up in the session that added it — a reader not told that thinks it is broken.
    assert "SECONDARY, FOR THE ADVANCED READER: `mcp_command`, quoted exactly" in beat
    assert "would rather stay in the session they already have open" in beat
    assert "the tools arrive in a NEW session" in beat
    assert "they add the server, find no tools, and think it is broken" in beat
    assert "Say it second and say it shorter" in beat
    # The paste-into-your-assistant message is gone from the tour with its field.
    assert "mcp_ask_your_assistant" not in _flat(TUTORIAL_SYSTEM_PROMPT)
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
        | set(TutorialAgentReference.model_fields)
        | set(Project.model_fields)
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

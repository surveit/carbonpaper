"""The tour's script, as prose the model obeys: it names no tool or field the tour
does not hold, and it covers the beats the tour is supposed to walk."""
from __future__ import annotations

import re
from pathlib import Path

import app
from app.agents.tutorial.config import build_tutorial_tools
from app.agents.tutorial.prompt import TUTORIAL_OPENING_MESSAGE, TUTORIAL_SYSTEM_PROMPT
from app.core.run_status import RunStatus
from app.models import StageType
from app.models.stage_contribution import QueueStats
from app.models.records.run_manifest import RunManifest
from app.services.project import WorkflowFile
from app.models.records.project import Project
from app.services.workspace import StageSummary
from app.tools.shared import StageOutputRow, StageOutputRows
from app.tools.tool_specs import find_tool_names
from app.tools.tutorial import _FIXTURE, TutorialAgentReference, TutorialContext
from app.web.breadcrumbs import _HOME_LABEL

_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")
_TEMPLATES = Path(app.__file__).resolve().parent / "templates"

# Labels the tour sends the reader to click. Each must be a string the app renders.
_NAMED_CONTROLS = ("Export review packet",)


def _tour_tool_names() -> set[str]:
    return {
        spec.name
        for spec in build_tutorial_tools(TutorialContext(base_url="http://x/"))
    }


def _tour_tool_arguments() -> set[str]:
    return {
        argument
        for spec in build_tutorial_tools(TutorialContext(base_url="http://x/"))
        for argument in spec.json_schema.get("properties", {})
    }


def _rendered_templates() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(_TEMPLATES.glob("*.html"))
    )


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


def test_the_opening_message_ends_on_the_question() -> None:
    """registry.render_system_prompt re-appends it; this file just owns the words."""
    assert "Ready to get started?" in TUTORIAL_OPENING_MESSAGE
    assert TUTORIAL_OPENING_MESSAGE not in TUTORIAL_SYSTEM_PROMPT


def test_the_prompt_writes_the_product_name_the_page_around_it_writes() -> None:
    """Read off the breadcrumb: the header names the product while the tour is speaking."""
    assert _HOME_LABEL in TUTORIAL_SYSTEM_PROMPT
    for wrong in ("carbonpaper", "Carbonpaper", "CarbonPaper"):
        assert wrong not in TUTORIAL_SYSTEM_PROMPT


def test_the_prompt_names_no_tool_the_tour_does_not_hold() -> None:
    known = find_tool_names() | _tour_tool_names()
    # A retired tool still named in the script reads to the model as an instruction.
    named = set(_IDENTIFIER.findall(TUTORIAL_SYSTEM_PROMPT)) & known

    assert named <= _tour_tool_names(), sorted(named - _tour_tool_names())
    assert {"create_tutorial_project", "run_workflow", "get_run_status"} <= named


def test_the_prompt_says_the_data_is_real_public_record() -> None:
    assert "public" in TUTORIAL_SYSTEM_PROMPT.lower()
    assert "real" in TUTORIAL_SYSTEM_PROMPT.lower()


def test_the_prompt_covers_the_five_requested_beats() -> None:
    # "What Carbon Paper is" is the opening message's job now, not the system prompt's.
    assert "Carbon Paper exists because" in TUTORIAL_OPENING_MESSAGE

    prompt = TUTORIAL_SYSTEM_PROMPT
    assert "Seed it" in prompt
    assert "Projects and workflows" in prompt
    assert "review queue" in prompt.lower()
    assert "Lineage" in prompt
    assert "Export" in prompt
    assert "Evals" in prompt
    assert "Get them started" in prompt


def test_no_fabrication_rule_survives() -> None:
    assert "Never state a number, row count, or fact you did not just read" in TUTORIAL_SYSTEM_PROMPT


def test_the_prompt_tells_the_model_not_to_repeat_the_canned_greeting() -> None:
    """No turn exists for the greeting, so the model must be told it already happened."""
    prompt = " ".join(TUTORIAL_SYSTEM_PROMPT.split())

    assert "Do not repeat the greeting" in prompt
    assert 'treat it as "yes"' in prompt


def test_every_control_the_tour_sends_them_to_click_is_one_the_app_renders() -> None:
    """A button named here that does not exist sends the reader looking for nothing."""
    rendered = _rendered_templates()

    for label in _NAMED_CONTROLS:
        assert label in TUTORIAL_SYSTEM_PROMPT, label
        assert label in rendered, f"{label} is named in the tour but rendered nowhere"


def test_every_name_the_prompt_quotes_is_one_the_code_defines() -> None:
    """A renamed stage, column, field or argument leaves the prompt pointing at nothing."""
    fixture = WorkflowFile.model_validate_json(_FIXTURE.read_text(encoding="utf-8"))
    run_status_words = {status.value for status in RunStatus} | {"status", "error"}
    known = (
        {stage.id for stage in fixture.stages}
        | _fixture_column_names(fixture)
        | set(TutorialAgentReference.model_fields)
        | set(Project.model_fields)
        | set(StageOutputRow.model_fields)
        | set(StageOutputRows.model_fields)
        | set(StageSummary.model_fields)
        | {stage_type.value for stage_type in StageType}
        | _tour_tool_names()
        | _tour_tool_arguments()
        | set(RunManifest.model_fields)
        | set(QueueStats.__annotations__)
        | run_status_words
    )
    quoted = set(re.findall(r"`([a-z][a-z0-9_]{3,})`", TUTORIAL_SYSTEM_PROMPT))

    assert quoted <= known, sorted(quoted - known)

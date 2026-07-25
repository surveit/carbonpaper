"""Compile a methodology document into a WORKFLOW (a validated `Workflow`).

The sibling of `app.compiler.data_model` (prose → named schemas): this is prose → typed
stages. It builds an `app.core.agent.agent.Agent` whose target schema is `Workflow`, so the agent SUBMITS
the workflow through the submit_answer tool — validated against `Workflow` (each stage's own
invariants + the cross-stage graph checks) — and a schema-invalid draft comes back as a tool
error the agent corrects IN THE SAME LOOP.

When a `data_model` (the approved SchemaLibrary) is given, its named schemas ground the task as
the nouns the workflow imports and generates. Running the agent and persisting what it submits
are the caller's job.
"""
from __future__ import annotations

import json
from typing import Any

from app.compiler.workflow_prompt import WORKFLOW_SYSTEM_PROMPT
from app.core.agent.agent import Agent
from app.models.named_schemas import SchemaLibrary
from app.models.workflow import Workflow
from app.services.loader import stage_to_spec_dict


def build_workflow_agent(
    document: str, *, data_model: SchemaLibrary | None = None, model: str = "sonnet"
) -> Agent[Workflow]:
    """Configure the workflow agent for `document`: it distils the process into typed stages
    and SUBMITS them as a `Workflow` via submit_answer — validated (each stage's own invariants
    + the cross-stage graph checks), so a schema-invalid draft comes back as a tool error the
    agent corrects until the workflow is clean. When `data_model` (the approved schemas) is
    given, it grounds the task as the nouns the workflow imports and generates. Read `.answer`
    after the run/turn for the validated Workflow (None if nothing valid was submitted)."""
    return Agent(
        system_prompt=WORKFLOW_SYSTEM_PROMPT,
        target_schema=Workflow,
        task=_build_initial_message(document, data_model),
        model=model,
    )


def _build_initial_message(document: str, data_model: SchemaLibrary | None) -> str:
    """Build the agent's initial user message: the document (and, when grounded, the approved
    data model) as the material to compile, delimited so the agent treats it as source, not
    instructions."""
    grounding = ""
    if data_model is not None:
        grounding = "\n\n" + _render_data_model_reference(data_model)
    return (
        "Here is the methodology document. Compile it into a workflow of typed stages and "
        "submit it with submit_answer.\n\n"
        "----- DOCUMENT -----\n"
        f"{document}\n"
        "----- END DOCUMENT -----"
        f"{grounding}"
    )


def _render_data_model_reference(data_model: SchemaLibrary) -> str:
    """Render the reviewed data model as a reference block: the named schemas verbatim, framed
    as the nouns the workflow is grounded in (not a governing constraint), with the instruction
    to note where the workflow diverges from or extends them."""
    schemas_json = json.dumps(
        [s.model_dump(mode="json", exclude_none=True) for s in data_model.schemas],
        indent=2,
        ensure_ascii=False,
    )
    return (
        "# Data model — the nouns this workflow is grounded in\n"
        "The named schemas below are the nouns the workflow IMPORTS and GENERATES — the\n"
        "reviewed, agreed-upon entities that intellectually ground this pipeline. They ground\n"
        "the workflow; they do not rigidly govern it: the workflow may introduce intermediate\n"
        "objects it needs, or extend these nouns with extra fields. Start from them as the\n"
        "canonical entities, and note where you diverge from or extend them, and why.\n\n"
        f"{schemas_json}"
    )


def workflow_result(workflow: Workflow, name: str) -> dict[str, Any]:
    """Shape a validated Workflow into the dict write_methodology persists: the stages in
    canonical on-disk form, with a clean validation list (the agent only submits a workflow that
    already validates). The agent carries the shape through the tool, so there is no prose
    methodology_raw write-up and no top-level compiler_notes — any per-stage notes ride along on
    each stage."""
    return {
        "name": name,
        "stages": [stage_to_spec_dict(s) for s in workflow.stages],
        "methodology_raw": "",
        "compiler_notes": None,
        "validation": [],
    }

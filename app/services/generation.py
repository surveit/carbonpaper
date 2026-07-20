"""Auto-generation: on a fresh document, generate the DATA MODEL as a live chat turn and write
the schemas. The WORKFLOW is NOT auto-built — the create-flow stops at the data model so it can
be reviewed and approved first; the workflow is then generated on demand (grounded on that
approved model) via start_workflow_generation. STAGE TESTS are generated per stage, on demand,
via start_stage_test_generation.

These turns run through the app.compiler bridges (start_data_model_generation_agent /
start_workflow_generation_agent / start_stage_test_derivation_agent):
app.compiler owns the app.core.agent spine, so this orchestration delegates there rather than
importing the spine directly. `start_generation` streams the data-model agent to /chat/<sid>;
on a valid submission its schemas are written. `start_workflow_generation` is the manual
workflow build — clicking "Generate workflow" runs the workflow agent as a live turn and
returns its session id (the route lands the user on /chat/<sid>); it compiles ONLY the
workflow, grounding it in the approved data model, without touching schemas/.
`start_stage_test_generation` runs the deriver agent for one python-transform stage as a
HIDDEN, view-only turn and, on completion, REPLACES that stage's tests wholesale. A phase
that fails is surfaced in the live turn / logged, never fabricated as success.

The turns run on the server event loop, so every `start_*` entry here must be called from an
async context. The CLI subprocess the agents spawn runs with the Claude-Code session markers
already stripped from os.environ (see app.compiler.compiler), imported transitively via the
bridges.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.compiler.data_model import start_data_model_generation_agent
from app.compiler.stage_tests import start_stage_test_derivation_agent
from app.compiler.workflow import start_workflow_generation_agent
from app.core.errors import GenerationError
from app.core.models.named_schemas import SchemaLibrary
from app.core.models.workflow import Workflow
from app.services import data_model
from app.services.compilation import regenerate_workflow
from app.services.loader import load_workflow, stage_to_spec_dict
from app.services.project import _document_path
from app.services.stage_edit import patch_stage_spec

_log = logging.getLogger(__name__)


def start_generation(project_dir: Path, *, document: str, model: str) -> str:
    """Kick off DATA-MODEL generation and return the id of the chat session streaming the
    conversation. The data-model agent runs as a LIVE turn (watchable at /chat/<sid>, persisted
    when it ends); on a valid submission its schemas are written. The workflow is NOT auto-built
    — the create-flow stops at the data model so it can be reviewed/approved first. Must be
    called from the server event loop — the underlying turn is started there."""
    return start_data_model_generation_agent(
        document=document,
        project_name=project_dir.name,
        model=model,
        on_answer=lambda answer: _finish_data_model(project_dir, answer),
    )


def start_workflow_generation(
    project_dir: Path,
    *,
    document: str,
    model: str,
    data_model: SchemaLibrary | None,
) -> str:
    """Run the WORKFLOW agent as a LIVE chat turn and return its session id (the caller lands
    the user on /chat/<sid>). Compiles ONLY the workflow — schemas/ is untouched — grounding it
    in `data_model` (the approved schemas) when given. Must be called from the server event
    loop."""
    name = project_dir.name
    return start_workflow_generation_agent(
        document=document,
        project_name=name,
        model=model,
        data_model=data_model,
        on_answer=lambda answer: _finish_workflow(project_dir, name, answer),
    )


def start_stage_test_generation(project_dir: Path, *, stage_id: str, model: str) -> str:
    """Kick off STAGE-TEST derivation for one python-transform stage and return the id of
    the (hidden, view-only) chat session streaming the turn. Loads document.md and the
    stage's current compiled spec — raising ValueError if the project has no document, or
    `stage_id` names no stage in the compiled workflow (a wrong stage TYPE is instead
    caught by the deriver itself, inside build_stage_test_deriver). On completion,
    `_finish_stage_tests` REPLACES the stage's tests wholesale with whatever suite the
    agent submitted — no human-touched marker exists yet, so this is a destructive
    regenerate (documented on the generate-tests button/route). Must be called from the
    server event loop."""
    doc_path = _document_path(project_dir)
    if doc_path is None:
        raise ValueError(f"{project_dir.name} has no document to derive tests from")
    stages = {stage.id: stage for stage in load_workflow(project_dir)}
    stage = stages.get(stage_id)
    if stage is None:
        raise ValueError(f"no stage '{stage_id}' in {project_dir.name}")
    return start_stage_test_derivation_agent(
        document=doc_path.read_text(encoding="utf-8"),
        stage=stage,
        project_id=project_dir.name,
        model=model,
        on_answer=lambda answer: _finish_stage_tests(project_dir, stage_id, answer),
    )


def _finish_data_model(project_dir: Path, answer: SchemaLibrary | None) -> None:
    """Completion hook for the data-model turn (runs on the event loop): if the agent submitted
    a valid data model (`answer`), persist the schemas. The create-flow stops here — the
    workflow is built later, on demand, from the reviewed data model. A failed submission was
    already streamed to the live turn; there is nothing to persist."""
    if answer is None:
        return
    data_model.write_data_model(project_dir, answer)


def _finish_workflow(project_dir: Path, name: str, answer: Workflow | None) -> None:
    """Completion hook for the workflow turn: if the agent submitted a valid Workflow, write it
    (schemas/ untouched); otherwise the failure was already streamed to the live turn."""
    if answer is None:
        return
    regenerate_workflow(_workflow_result(answer, name), project_dir)


def _finish_stage_tests(project_dir: Path, stage_id: str, answer: BaseModel | None) -> None:
    """Completion hook for the stage-test-derivation turn (runs on the event loop):
    REPLACES `stage_id`'s tests wholesale with the submitted suite — the whole `tests`
    array, not a merge of individual cases, since no human-touched marker exists yet to
    tell an authored case from a stale one (that arrives with the interactive-editor
    sub-slice; until then, generate-tests is an explicitly destructive regenerate).

    Fails loudly rather than writing on a doubt: `answer is None` (the agent never
    submitted) raises GenerationError, and a patch that stage_edit.patch_stage_spec
    refuses (it validates the whole resulting workflow before writing) raises
    GenerationError naming the reported issues — never a silent no-op."""
    if answer is None:
        raise GenerationError(
            f"stage-test derivation for '{stage_id}' in {project_dir.name} "
            "did not submit a suite"
        )
    patch_text = json.dumps(answer.model_dump(by_alias=True, exclude_none=True))
    result = patch_stage_spec(project_dir, stage_id, patch_text)
    if not result.ok:
        raise GenerationError(
            f"stage-test derivation for '{stage_id}' in {project_dir.name} "
            "failed to patch: " + "; ".join(result.issues)
        )


def _workflow_result(workflow: Workflow, name: str) -> dict[str, Any]:
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

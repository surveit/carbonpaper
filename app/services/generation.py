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
that fails is never fabricated as success: the error streams to the live turn AND is
persisted into the session's transcript (app.compiler.stage_tests), so it is visible on
reload even to a caller who was not watching live.

The turns run on the server event loop, so every `start_*` entry here must be called from an
async context. The CLI subprocess the agents spawn runs with the Claude-Code session markers
already stripped from os.environ (see app.compiler.compiler), imported transitively via the
bridges.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel

from app.compiler.data_model import start_data_model_generation_agent
from app.compiler.stage_tests import start_stage_test_derivation_agent
from app.compiler.workflow import workflow_result, start_workflow_generation_agent
from app.core.errors import GenerationError
from app.models.named_schemas import SchemaLibrary
from app.models.stages.stage_tests import STAGE_TEST_TYPES
from app.models.workflow import Workflow
from app.services import data_model
from app.services.compilation import regenerate_workflow
from app.services.loader import load_workflow
from app.services.project import find_document_path
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
    stage's current compiled spec — raising ValueError if the project has no document,
    `stage_id` names no stage in the compiled workflow, the stage is not a python
    transform, or the stage has no output_schema (a python transform may validly lack one,
    but tests need it to state expected rows). Every one of these checks runs BEFORE the
    session/turn are started, so a rejected stage never creates an orphaned session
    (build_stage_test_deriver / render_derivation_task would raise the same errors, but only
    after the session already exists). On completion, `_finish_stage_tests`
    REPLACES the stage's tests wholesale with whatever suite the agent submitted — no
    human-touched marker exists yet, so this is a destructive regenerate (documented on the
    generate-tests button/route). Must be called from the server event loop."""
    doc_path = find_document_path(project_dir)
    if doc_path is None:
        raise ValueError(f"{project_dir.name} has no document to derive tests from")
    stages = {stage.id: stage for stage in load_workflow(project_dir)}
    stage = stages.get(stage_id)
    if stage is None:
        raise ValueError(f"no stage '{stage_id}' in {project_dir.name}")
    if stage.type not in STAGE_TEST_TYPES:
        raise ValueError(
            f"tests can only be derived for python transforms, not `{stage.type}`"
        )
    if stage.output_schema is None:
        raise ValueError(
            f"stage `{stage_id}` has no output schema — tests need one to state expected rows"
        )
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
    regenerate_workflow(workflow_result(answer, name), project_dir)


def _finish_stage_tests(project_dir: Path, stage_id: str, answer: BaseModel | None) -> None:
    """Completion hook for the stage-test-derivation turn (runs on the event loop):
    REPLACES `stage_id`'s tests wholesale with the submitted suite — the whole `tests`
    array, not a merge of individual cases, since no human-touched marker exists yet to
    tell an authored case from a stale one.

    Fails loudly rather than writing on a doubt: `answer is None` (the agent never
    submitted) or an empty `tests` array (the agent submitted a suite with no cases,
    which would silently wipe any existing tests) both raise GenerationError, and a
    patch that stage_edit.patch_stage_spec refuses (it validates the whole resulting
    workflow before writing) raises GenerationError naming the reported issues —
    never a silent no-op. Either GenerationError is caught by the caller
    (start_stage_test_derivation_agent's on_done hook), which persists it into the
    session's transcript before re-raising."""
    if answer is None:
        raise GenerationError(
            f"stage-test derivation for '{stage_id}' in {project_dir.name} "
            "did not submit a suite"
        )
    patch = answer.model_dump(mode="json", by_alias=True, exclude_none=True)
    if not patch.get("tests"):
        raise GenerationError(
            f"stage-test derivation for '{stage_id}' in {project_dir.name} "
            "submitted an empty test suite"
        )
    patch_text = json.dumps(patch)
    result = patch_stage_spec(project_dir, stage_id, patch_text)
    if not result.ok:
        raise GenerationError(
            f"stage-test derivation for '{stage_id}' in {project_dir.name} "
            "failed to patch: " + "; ".join(result.issues)
        )

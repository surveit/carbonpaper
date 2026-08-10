"""LLM generation of a project's data model, of one stage's tests, and of one saved
version's review guide. The turns run on the server event loop, so every `start_*` entry
here must be called from an async context. Stage-test generation REPLACES that stage's
tests wholesale; guide generation refuses a version that already carries one.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel

from app.compiler.data_model import start_data_model_generation_agent
from app.compiler.review_guide import start_review_guide_generation_agent
from app.compiler.stage_tests import start_stage_test_generation_agent
# Re-exported for the status route: only services may import app.compiler, and the
# route must match the marker on the same string the turn writes.
from app.compiler.turn_failure import GENERATION_FAILURE_PREFIX as GENERATION_FAILURE_PREFIX
from app.core.errors import GenerationError
from app.models.review_guide import ReviewGuideDraft
from app.models.named_schemas import SchemaLibrary
from app.services import data_model, versioning
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


def start_stage_test_generation(project_dir: Path, *, stage_id: str, model: str) -> str:
    """Kick off STAGE-TEST generation for one stage and return the id of
    the (hidden, view-only) chat session streaming the turn. Loads document.md and the
    stage's current compiled spec — raising ValueError if the project has no document,
    `stage_id` names no stage in the compiled workflow, the stage's type carries no
    runnable tests, or the stage resolves no output schema (tests need one to state
    expected rows).
    Every one of these checks runs BEFORE the
    session/turn are started, so a rejected stage never creates an orphaned session
    (build_stage_test_generator / render_generation_task would raise the same errors, but only
    after the session already exists). On completion, `_finish_stage_tests`
    REPLACES the stage's tests wholesale with whatever suite the agent submitted — no
    human-touched marker exists yet, so this is a destructive regenerate (documented on the
    generate-tests button/route). Must be called from the server event loop."""
    doc_path = find_document_path(project_dir)
    if doc_path is None:
        raise ValueError(f"{project_dir.name} has no document to generate tests from")
    stages = {stage.id: stage for stage in load_workflow(project_dir.name)}
    stage = stages.get(stage_id)
    if stage is None:
        raise ValueError(f"no stage '{stage_id}' in {project_dir.name}")
    if not stage.CARRIES_RUNNABLE_TESTS:
        raise ValueError(
            f"tests can only be generated for stage types that can run them, "
            f"not `{stage.type}`"
        )
    if stage.resolve_output_schema() is None:
        raise ValueError(
            f"stage `{stage_id}` has no output schema — tests need one to state expected rows"
        )
    return start_stage_test_generation_agent(
        document=doc_path.read_text(encoding="utf-8"),
        stage=stage,
        project_id=project_dir.name,
        model=model,
        on_answer=lambda answer: _finish_stage_tests(project_dir, stage_id, answer),
    )


def start_review_guide_generation(
    project_dir: Path, *, version_id: str, model: str
) -> str:
    """Stages come off the VERSION, not the working copy. Must be called from the server
    event loop."""
    # Both refusals below run BEFORE the session is created, so neither leaves an
    # orphaned session behind.
    version = versioning.load_version(project_dir, version_id)
    existing = versioning.find_latest_review_guide(project_dir.name, version_id)
    if existing is not None:
        raise ValueError(
            f"version '{version_id}' already has a review guide — edit it with the "
            "authoring agent rather than regenerating over it"
        )
    doc_path = find_document_path(project_dir)
    if doc_path is None:
        raise ValueError(f"{project_dir.name} has no document to write a guide from")
    return start_review_guide_generation_agent(
        stages=version.stages,
        version_id=version.version_id,
        project_id=project_dir.name,
        document=doc_path.read_text(encoding="utf-8"),
        model=model,
        on_answer=lambda draft: _finish_review_guide(project_dir, version_id, draft),
    )


def _finish_data_model(project_dir: Path, answer: SchemaLibrary | None) -> None:
    """Completion hook for the data-model turn (runs on the event loop): if the agent submitted
    a valid data model (`answer`), persist the schemas. The create-flow stops here — the
    workflow is built later, on demand, from the reviewed data model. A failed submission was
    already streamed to the live turn; there is nothing to persist."""
    if answer is None:
        return
    data_model.write_data_model(project_dir.name, answer)


def _finish_review_guide(
    project_dir: Path, version_id: str, draft: ReviewGuideDraft | None
) -> None:
    """Completion hook for the guide turn; either raise below reaches the transcript via the
    caller."""
    if draft is None:
        raise GenerationError(
            f"review-guide generation for version '{version_id}' in {project_dir.name} "
            "did not submit a guide"
        )
    versioning.save_version_guide(
        project_dir,
        version_id,
        versioning.ReviewGuide(
            project=project_dir.name, version_id=version_id,
            steps=draft.steps, unnarrated=draft.unnarrated,
        ),
    )


def _finish_stage_tests(project_dir: Path, stage_id: str, answer: BaseModel | None) -> None:
    """Completion hook for the stage-test-generation turn (runs on the event loop):
    REPLACES `stage_id`'s tests wholesale with the submitted suite — the whole `tests`
    array, not a merge of individual cases, since no human-touched marker exists yet to
    tell an authored case from a stale one.

    Fails loudly rather than writing on a doubt: `answer is None` (the agent never
    submitted) or an empty `tests` array (the agent submitted a suite with no cases,
    which would silently wipe any existing tests) both raise GenerationError, and a
    patch that stage_edit.patch_stage_spec refuses (it validates the whole resulting
    workflow before writing) raises GenerationError naming the reported issues —
    never a silent no-op. Either GenerationError is caught by the caller
    (start_stage_test_generation_agent's on_done hook), which persists it into the
    session's transcript before re-raising."""
    if answer is None:
        raise GenerationError(
            f"stage-test generation for '{stage_id}' in {project_dir.name} "
            "did not submit a suite"
        )
    patch = answer.model_dump(mode="json", by_alias=True, exclude_none=True)
    if not patch.get("tests"):
        raise GenerationError(
            f"stage-test generation for '{stage_id}' in {project_dir.name} "
            "submitted an empty test suite"
        )
    patch_text = json.dumps(patch)
    result = patch_stage_spec(project_dir.name, stage_id, patch_text)
    if not result.ok:
        raise GenerationError(
            f"stage-test generation for '{stage_id}' in {project_dir.name} "
            "failed to patch: " + "; ".join(result.issues)
        )

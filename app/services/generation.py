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
from app.compiler.stage_tests import start_stage_test_derivation_agent
from app.core.errors import GenerationError
from app.models.review_guide import ReviewGuide
from app.models.named_schemas import SchemaLibrary
from app.services import data_model, versioning
from app.services.loader import load_workflow
from app.services.project import find_document_path
from app.services.stage_edit import patch_stage_spec

_log = logging.getLogger(__name__)


def start_generation(project_dir: Path, *, document: str, model: str) -> str:
    """The workflow is NOT auto-built — the create flow stops at the data model, to be reviewed."""
    return start_data_model_generation_agent(
        document=document,
        project_name=project_dir.name,
        model=model,
        on_answer=lambda answer: _finish_data_model(project_dir, answer),
    )


def start_stage_test_generation(project_dir: Path, *, stage_id: str, model: str) -> str:
    """Every check below runs before the session starts, so a rejected stage never orphans one."""
    doc_path = find_document_path(project_dir)
    if doc_path is None:
        raise ValueError(f"{project_dir.name} has no document to derive tests from")
    stages = {stage.id: stage for stage in load_workflow(project_dir)}
    stage = stages.get(stage_id)
    if stage is None:
        raise ValueError(f"no stage '{stage_id}' in {project_dir.name}")
    if not stage.CARRIES_RUNNABLE_TESTS:
        raise ValueError(
            f"tests can only be derived for stage types that can run them, "
            f"not `{stage.type}`"
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


def start_review_guide_generation(
    project_dir: Path, *, version_id: str, model: str
) -> str:
    """Stages come off the VERSION, not the working copy. Must be called from the server
    event loop."""
    # Both refusals below run BEFORE the session is created, so neither leaves an
    # orphaned session behind.
    version = versioning.load_version(project_dir, version_id)
    if version.guide is not None:
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
        on_answer=lambda guide: _finish_review_guide(project_dir, version_id, guide),
    )


def _finish_data_model(project_dir: Path, answer: SchemaLibrary | None) -> None:
    """A failed submission was already streamed to the live turn; there is nothing to persist."""
    if answer is None:
        return
    data_model.write_data_model(project_dir, answer)


def _finish_review_guide(
    project_dir: Path, version_id: str, guide: ReviewGuide | None
) -> None:
    """Completion hook for the guide turn; either raise below reaches the transcript via the
    caller."""
    if guide is None:
        raise GenerationError(
            f"review-guide generation for version '{version_id}' in {project_dir.name} "
            "did not submit a guide"
        )
    versioning.save_version_guide(project_dir, version_id, guide)


def _finish_stage_tests(project_dir: Path, stage_id: str, answer: BaseModel | None) -> None:
    """Replaces the stage's tests wholesale; an empty suite raises rather than silently wiping."""
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

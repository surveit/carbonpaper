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
from app.models.stage import stage_to_spec_dict
from app.services import terms, versioning
from app.services.loader import load_workflow
from app.services.project import find_document_path
from app.services.stage_edit import (
    find_description_issues,
    find_unnamed_model_issues,
    patch_stage_spec,
)

_log = logging.getLogger(__name__)


def start_generation(project_dir: Path, *, document: str, model: str) -> str:
    return start_data_model_generation_agent(
        document=document,
        project_name=project_dir.name,
        model=model,
        on_answer=lambda answer: _finish_data_model(project_dir, answer),
    )


def start_stage_test_generation(project_dir: Path, *, stage_id: str, model: str) -> str:
    """Every check runs before the turn starts, so a rejected stage leaves no orphaned session."""
    stages = {stage.id: stage for stage in load_workflow(project_dir.name)}
    stage = stages.get(stage_id)
    if stage is None:
        raise ValueError(f"no stage '{stage_id}' in {project_dir.name}")
    if not stage.CARRIES_RUNNABLE_TESTS:
        raise ValueError(
            f"tests can only be generated for stage types that can run them, "
            f"not `{stage.type}`"
        )
    # Asked here because _finish_stage_tests writes the suite back through
    # patch_stage_spec, which asks the same question — and a turn that cannot be
    # persisted must not be paid for first.
    spec = stage_to_spec_dict(stage)
    unwritable = find_description_issues(spec) + find_unnamed_model_issues(spec)
    if unwritable:
        raise ValueError(
            f"stage `{stage_id}` cannot be written back as it stands, so a generated "
            f"suite could not be saved: " + "; ".join(unwritable)
        )
    return start_stage_test_generation_agent(
        terms=terms.load_terms(project_dir.name),
        stage=stage,
        project_id=project_dir.name,
        model=model,
        on_answer=lambda answer: _finish_stage_tests(project_dir, stage_id, answer),
    )


def start_review_guide_generation(
    project_dir: Path, *, version_id: str, model: str
) -> str:
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
        terms=terms.load_terms(project_dir.name),
        model=model,
        on_answer=lambda draft: _finish_review_guide(project_dir, version_id, draft),
    )


def _finish_data_model(project_dir: Path, answer: SchemaLibrary | None) -> None:
    if answer is None:
        return
    terms.write_nouns(project_dir.name, answer)


def _finish_review_guide(
    project_dir: Path, version_id: str, draft: ReviewGuideDraft | None
) -> None:
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
    if answer is None:
        raise GenerationError(
            f"stage-test generation for '{stage_id}' in {project_dir.name} "
            "did not submit a suite"
        )
    patch = answer.model_dump(mode="json", by_alias=True, exclude_none=True)
    # The patch replaces the `tests` array wholesale, so an empty suite would wipe
    # whatever the stage already had.
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

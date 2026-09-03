"""LLM generation of a project's data model, of one stage's tests, and of one saved
version's review guide. The turns run on the server event loop, so every `start_*` entry
here must be called from an async context. Stage-test generation REPLACES that stage's
tests wholesale; guide generation refuses a version that already carries one.
"""
from __future__ import annotations

import json
import logging

from pydantic import BaseModel

from app.compiler.data_model import start_data_model_generation_agent
from app.compiler.review_guide import start_review_guide_generation_agent
from app.compiler.stage_tests import start_stage_test_generation_agent
from app.compiler.stage_tests_submission import dump_submitted_tests, read_selected_rows
# Re-exported for the status route: only services may import app.compiler, and the
# route must match the marker on the same string the turn writes.
from app.compiler.turn_failure import GENERATION_FAILURE_PREFIX as GENERATION_FAILURE_PREFIX
from app.core.errors import GenerationError
from app.core.row_search import InputRows
from app.models.review_guide import ReviewGuideDraft
from app.models.named_schemas import SchemaLibrary
from app.models.schema import StageId
from app.models.stage import StageEdit, stage_to_spec_dict
from app.models.stages.signature import transform_input_schemas
from app.models.stages.stage_base import find_stage_test_class
from app.models.stages.stage_tests import StageTest
from app.services import terms, versioning
from app.services.loader import load_workflow
from app.services.methodology import read_methodology
from app.services.stage_edit import (
    open_working_copy,
    find_description_issues,
    find_unnamed_model_issues,
    patch_stage_specs,
)
from app.services.stage_test_rows import load_stage_row_sources

_log = logging.getLogger(__name__)


def start_generation(project_id: str, *, document: str, model: str) -> str:
    return start_data_model_generation_agent(
        document=document,
        project_name=project_id,
        model=model,
        on_answer=lambda answer: _finish_data_model(project_id, answer),
    )


def start_stage_test_generation(project_id: str, *, stage_id: str, model: str) -> str:
    """Every check runs before the turn starts, so a rejected stage leaves no orphaned session."""
    stages = {stage.id: stage for stage in load_workflow(project_id)}
    stage = stages.get(stage_id)
    if stage is None:
        raise ValueError(f"no stage '{stage_id}' in {project_id}")
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
    # Loaded here, before the turn: examples are selected from real rows, so a project
    # with none refuses without paying for an agent that could select nothing.
    sources = load_stage_row_sources(
        project_id, transform_input_schemas(stage))
    test_class = find_stage_test_class(type(stage))
    return start_stage_test_generation_agent(
        terms=terms.load_terms(project_id),
        stage=stage,
        sources=sources,
        project_id=project_id,
        model=model,
        on_answer=lambda answer: _finish_stage_tests(
            project_id, stage_id, answer, test_class, sources),
    )


def start_review_guide_generation(
    project_id: str, *, version_id: str, model: str
) -> str:
    version = versioning.load_version(project_id, version_id)
    existing = versioning.find_latest_review_guide(project_id, version_id)
    if existing is not None:
        raise ValueError(
            f"version '{version_id}' already has a review guide — edit it with the "
            "authoring agent rather than regenerating over it"
        )
    document = read_methodology(project_id)
    if document is None:
        raise ValueError(f"{project_id} has no document to write a guide from")
    return start_review_guide_generation_agent(
        stages=version.stages,
        version_id=version.version_id,
        project_id=project_id,
        document=document,
        terms=terms.load_terms(project_id),
        model=model,
        on_answer=lambda draft: _finish_review_guide(project_id, version_id, draft),
    )


def _finish_data_model(project_id: str, answer: SchemaLibrary | None) -> None:
    if answer is None:
        return
    terms.write_nouns(project_id, answer)


def _finish_review_guide(
    project_id: str, version_id: str, draft: ReviewGuideDraft | None
) -> None:
    if draft is None:
        raise GenerationError(
            f"review-guide generation for version '{version_id}' in {project_id} "
            "did not submit a guide"
        )
    versioning.save_version_guide(
        project_id,
        version_id,
        versioning.ReviewGuide(
            project=project_id, version_id=version_id,
            steps=draft.steps, unnarrated=draft.unnarrated,
        ),
    )


def _finish_stage_tests(
    project_id: str,
    stage_id: str,
    answer: BaseModel | None,
    test_class: type[StageTest],
    sources: dict[StageId, InputRows],
) -> None:
    if answer is None:
        raise GenerationError(
            f"stage-test generation for '{stage_id}' in {project_id} "
            "did not submit a suite"
        )
    # What is stored is the rows READ OFF the run, not the selections the agent sent:
    # the same reading its submission was validated against.
    submitted = getattr(answer, "tests", [])
    tests = dump_submitted_tests(read_selected_rows(submitted, test_class, sources))
    # The patch replaces the `tests` array wholesale, so an empty suite would wipe
    # whatever the stage already had.
    if not tests:
        raise GenerationError(
            f"stage-test generation for '{stage_id}' in {project_id} "
            "submitted an empty test suite"
        )
    result = patch_stage_specs(
        open_working_copy(project_id), [StageEdit(stage_id=stage_id, changes_json=json.dumps({"tests": tests}))]
    )
    if not result.ok:
        raise GenerationError(
            f"stage-test generation for '{stage_id}' in {project_id} "
            "failed to patch: " + "; ".join(result.issues)
        )

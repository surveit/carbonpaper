"""Immutable snapshots of a workflow, and the guide authored about one."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.core.errors import DocumentNotFound, NoVersionToRunError, ReviewGuideValidationError
from app.core.timestamp_ids import mint_timestamp_id
from app.models import Stage
from app.models.workflow import find_stages_reaching_report, parse_workflow
from app.models.records.review_guide import ReviewGuide
from app.models.records.workflow_version import WorkflowVersion
from app.core.utils import format_errors
from app.services.errors import WorkflowLoadError
from app.services.terms import load_terms


MAX_MESSAGE_CHARS = 150


def create_version_from_stages(
    project_id: str,
    stages: list[dict[str, Any]],
    *,
    message: str,
    parent_version: str | None = None,
) -> WorkflowVersion:
    if len(message) > MAX_MESSAGE_CHARS:
        raise WorkflowLoadError(
            f"{project_id}: version description",
            [f"{len(message)} characters; a description names the version in "
             f"{MAX_MESSAGE_CHARS} or fewer"],
        )
    workflow = parse_workflow(stages)

    version_id = mint_timestamp_id()
    schemas = [noun.model_dump(mode="json", exclude_none=True)
               for noun in load_terms(project_id).nouns.schemas]
    doc_id = f"{project_id}/{version_id}"

    v = WorkflowVersion(
        id=doc_id,
        version_id=version_id,
        parent_version=parent_version,
        message=message,
        stages=workflow.stages,
        schemas=schemas,
    )
    v.save()
    return v


def save_version_guide(
    project_id: str, version_id: str, guide: ReviewGuide
) -> ReviewGuide:
    _validate_guide_describes_the_version(guide, project_id, version_id)
    # The version is read for the frozen stages validation needs — which is also
    # what makes a guide for a version nobody stored fail here. It is only read:
    # the guide is its own document, so writing one leaves the snapshot untouched.
    version = load_version(project_id, version_id)
    validate_review_guide(guide, version.stages)
    guide.save()
    return guide


def find_latest_review_guide(project_id: str, version_id: str) -> ReviewGuide | None:
    guides = ReviewGuide.find(project=project_id, version_id=version_id)
    guides.sort(key=lambda g: (g.created_at, g.id), reverse=True)
    return guides[0] if guides else None


def validate_review_guide(guide: ReviewGuide, stages: list[Stage]) -> None:
    refusals = [
        *_describe_stage_mismatch(guide, stages),
        *_describe_unnarrated_stages_reaching_report(guide, stages),
        *_describe_sections_missing_data_description(guide),
    ]
    if refusals:
        raise ReviewGuideValidationError(" ".join(refusals))


def _describe_unnarrated_stages_reaching_report(
    guide: ReviewGuide, stages: list[Stage]
) -> list[str]:
    hidden = sorted(set(guide.unnarrated) & find_stages_reaching_report(stages))
    if not hidden:
        return []
    return [
        f"stage(s) listed unnarrated whose output reaches a report stage: {hidden} — "
        "each one's work is carried into the published files, so a reader checking a "
        "published figure may have to check it, and leaving it out of the walkthrough "
        "hides it. Narrate each in a section. `unnarrated` is only for a stage that "
        "reaches NO report stage."
    ]


def _describe_stage_mismatch(guide: ReviewGuide, stages: list[Stage]) -> list[str]:
    issues = _find_review_guide_issues(guide, stages)
    if not issues:
        return []
    return ["review guide does not match the version's stages: " + "; ".join(issues)]


def _describe_sections_missing_data_description(guide: ReviewGuide) -> list[str]:
    missing = [
        f"{position} ({step.title!r})"
        for position, step in enumerate(guide.steps, start=1)
        if not (step.data_description or "").strip()
    ]
    if not missing:
        return []
    return [
        "every Workflow section must carry `data_description`, one short sentence "
        "saying what the rows leaving it ARE; write one for section(s) "
        + ", ".join(missing) + "."
    ]


def _validate_guide_describes_the_version(
    guide: ReviewGuide, project_id: str, version_id: str
) -> None:
    if guide.project != project_id or guide.version_id != version_id:
        raise ValueError(
            f"guide describes {guide.project}/{guide.version_id}, not the version it is "
            f"being saved for: {project_id}/{version_id}"
        )


def _find_review_guide_issues(guide: ReviewGuide, stages: list[Stage]) -> list[str]:
    stage_ids = [stage.id for stage in stages]
    return (
        _find_unknown_stage_ids(guide, stage_ids)
        + _find_unaccounted_stage_ids(guide, stage_ids)
        + _find_doubly_placed_stage_ids(guide)
        + _find_repeated_step_stage_ids(guide)
    )


def _find_unknown_stage_ids(guide: ReviewGuide, stage_ids: list[str]) -> list[str]:
    known = set(stage_ids)
    named = [*guide.collect_step_stage_ids(), *guide.unnarrated]
    unknown = sorted({stage_id for stage_id in named if stage_id not in known})
    if not unknown:
        return []
    return [f"names stage id(s) this version does not have: {unknown}"]


def _find_unaccounted_stage_ids(guide: ReviewGuide, stage_ids: list[str]) -> list[str]:
    placed = {*guide.collect_step_stage_ids(), *guide.unnarrated}
    unaccounted = [stage_id for stage_id in stage_ids if stage_id not in placed]
    if not unaccounted:
        return []
    return [f"stage(s) in no step and not listed unnarrated: {unaccounted}"]


def _find_doubly_placed_stage_ids(guide: ReviewGuide) -> list[str]:
    both = sorted(set(guide.collect_step_stage_ids()) & set(guide.unnarrated))
    if not both:
        return []
    return [f"stage(s) narrated by a step AND listed unnarrated: {both}"]


def _find_repeated_step_stage_ids(guide: ReviewGuide) -> list[str]:
    named = guide.collect_step_stage_ids()
    repeated = sorted({stage_id for stage_id in named if named.count(stage_id) > 1})
    if not repeated:
        return []
    return [f"stage(s) narrated more than once across the steps: {repeated}"]


def _invalid_version_document(doc_id: str, exc: ValidationError) -> WorkflowLoadError:
    return WorkflowLoadError(f"version document {doc_id}", format_errors(exc))


def list_versions(project_id: str) -> list[WorkflowVersion]:
    versions: list[WorkflowVersion] = []
    for doc_id, data in WorkflowVersion.list_raw(f"{project_id}/"):
        try:
            v = WorkflowVersion.model_validate(data)
        except ValidationError as exc:
            raise _invalid_version_document(doc_id, exc) from exc
        versions.append(v)
    # version ids are strftime timestamps, so a reverse string sort on version_id
    # is chronological.
    versions.sort(key=lambda v: v.version_id, reverse=True)
    return versions


def find_latest_version_id(project_id: str) -> str | None:
    versions = list_versions(project_id)  # newest-first
    return versions[0].version_id if versions else None


def resolve_version_id(project_id: str, version_id: str | None) -> str:
    if version_id is not None:
        # load_version fails loudly on a missing version, and on one whose stored
        # document no longer validates — a caller asking for a specific id must
        # not be silently redirected to some other snapshot.
        load_version(project_id, version_id)
        return version_id

    latest = find_latest_version_id(project_id)
    if latest is None:
        # Never immortalise the working copy as a version to have something to run —
        # that is what let an invalid working copy poison "the latest" and fail every
        # subsequent run.
        raise NoVersionToRunError(
            f"No version to run for '{project_id}'. A run executes a stored "
            f"version and never creates one — save a version first."
        )
    return latest


def validate_version_exists(project_id: str, version_id: str) -> None:
    """Existence only: a stored version that no longer validates still passes."""
    if not WorkflowVersion.exists(f"{project_id}/{version_id}"):
        raise FileNotFoundError(f"No version '{version_id}' for project '{project_id}'")


def load_version(project_id: str, version_id: str) -> WorkflowVersion:
    return _load_version_document(project_id, version_id)


def load_version_stages(project_id: str, version_id: str) -> list[Stage]:
    return _load_version_document(project_id, version_id).stages


def _load_version_document(project_id: str, version_id: str) -> WorkflowVersion:
    doc_id = f"{project_id}/{version_id}"
    try:
        return WorkflowVersion.load(doc_id)
    except DocumentNotFound as exc:
        raise FileNotFoundError(f"No version '{version_id}' for project '{project_id}'") from exc
    except ValidationError as exc:
        raise _invalid_version_document(doc_id, exc) from exc

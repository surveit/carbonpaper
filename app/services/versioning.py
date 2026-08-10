"""Immutable, committable snapshots of a workflow, plus the guide authored about one.

`version_id` is the LOCAL id every public function here takes, never the composite
store id. A version is born unpublished, and a stored document carrying no
`published` key reads as unpublished."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from pydantic import Field, ValidationError

from app.core.errors import DocumentNotFound, NoVersionToRunError, ReviewGuideValidationError
from app.models import STAGE_SPEC_SCHEMA_VERSION, Stage
from app.models.review_guide import ReviewGuideStep
from app.models.workflow import find_stages_reaching_publish, parse_workflow
from app.core.persistence import PersistedModel, PersistenceScope, get_store
from app.core.utils import format_errors
from app.services.errors import WorkflowLoadError
from app.services.workspace import load_schemas


class WorkflowVersion(PersistedModel):
    """One frozen snapshot, stored in the "workflow_version" collection. `id` (inherited
    from PersistedModel) is the composite `f"{project}/{version_id}"`; `version_id`
    is the plain local id every caller of this module's public functions
    works with. `stages` and `schemas` are the frozen artifacts. `published`
    (plus `published_at`/`published_by`) records that a human reviewed this
    snapshot; it is a signal about the version, not a precondition for running it."""

    collection: ClassVar[str] = "workflow_version"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ
    SCHEMA_VERSION: ClassVar[int] = STAGE_SPEC_SCHEMA_VERSION
    # Dump the embedded stages in their spec-dict shape (field aliases
    # restored, unset optionals dropped) — the same convention stage_to_spec_dict
    # uses, so a version's on-disk stage shape matches the working copy's.
    DUMP_OPTS: ClassVar[dict[str, Any]] = {"by_alias": True, "exclude_none": True}

    version_id: str
    parent_version: str | None = None
    message: str
    reviewer: str
    stages: list[Stage] = Field(default_factory=list)
    schemas: list[dict[str, Any]] = Field(default_factory=list)
    published: bool = False
    published_at: str | None = None
    published_by: str | None = None


class ReviewGuide(PersistedModel):
    """`unnarrated` names the stages no step covers, so leaving one out is a decision."""

    collection: ClassVar[str] = "review_guide"
    # Authored prose ABOUT a version, written by save_version_guide alone: run
    # activity may read it and never writes one — WorkflowVersion's profile.
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    # `id` (inherited) autogenerates — nothing addresses a guide by a composed key.
    # It is found by these two backpointers instead: `project` prefixes the store
    # listing, `version_id` names the version whose stages the steps were validated
    # against. Writing a guide appends; the newest one for a version is the live one.
    project: str
    version_id: str
    # Prose only: a stage's name, type, order and columns are read off the
    # version's stages at render time rather than frozen a second time here.
    steps: list[ReviewGuideStep]
    unnarrated: list[str] = Field(default_factory=list)

    def collect_step_stage_ids(self) -> list[str]:
        """Every stage id the steps name, in step order, repeats included."""
        return [stage_id for step in self.steps for stage_id in step.stage_ids]


def create_version_from_stages(
    project_dir: Path,
    stages: list[dict[str, Any]],
    *,
    message: str,
    reviewer: str,
    parent_version: str | None = None,
) -> WorkflowVersion:
    """The single write chokepoint for a WorkflowVersion: strict-parse `stages`
    (raw spec dicts) as a whole Workflow, embed the project's CURRENT schemas, and
    save — born unpublished. Returns the saved WorkflowVersion.

    `stages` is parsed via app.models.workflow.parse_workflow, which raises
    pydantic.ValidationError (per-stage schema errors AND cross-stage graph
    issues alike) on anything invalid; nothing is written in that case. Every
    version is therefore a loadable workflow, from this seam or any other.

    schemas/ is read via workspace.load_schemas, which returns [] when the project
    has no schema library yet — a project with no data model still versions
    cleanly (the absence is truthful, not an error).

    version_id has 1-second resolution; two versions minted within the same
    wall-clock second for the same project collide on doc id, and the second
    save simply overwrites the first — an accepted same-second clobber, not
    guarded against."""
    project_dir = Path(project_dir)
    workflow = parse_workflow(stages)
    schemas = load_schemas(project_dir)

    version_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    project = project_dir.name
    doc_id = f"{project}/{version_id}"

    v = WorkflowVersion(
        id=doc_id,
        version_id=version_id,
        parent_version=parent_version,
        message=message,
        reviewer=reviewer,
        stages=workflow.stages,
        schemas=schemas,
        published=False,
    )
    v.save()
    return v


def publish_version(project_dir: Path, version_id: str, *, reviewer: str) -> WorkflowVersion:
    """Mark a version published: the metadata-only record that a human has looked
    at this snapshot. Running does not consult it — see resolve_version_id
    below. Idempotent — publishing an
    already-published version returns it unchanged, keeping the FIRST
    published_at/published_by rather than overwriting them with the second
    caller's. Fails loudly (FileNotFoundError) if no such version is stored, or
    if the stored document no longer validates (WorkflowLoadError)."""
    name = Path(project_dir).name
    doc_id = f"{name}/{version_id}"
    try:
        v = WorkflowVersion.load(doc_id)
    except DocumentNotFound as exc:
        raise FileNotFoundError(f"No version '{version_id}' for project '{name}'") from exc
    except ValidationError as exc:
        raise _invalid_version_document(doc_id, exc) from exc
    if v.published:
        return v
    v.published = True
    v.published_at = datetime.now().isoformat(timespec="seconds")
    v.published_by = reviewer
    v.save()
    return v


def save_version_guide(
    project_dir: Path, version_id: str, guide: ReviewGuide
) -> ReviewGuide:
    """Appends a guide; find_latest_review_guide then returns this one. Validates first."""
    _validate_guide_describes_the_version(guide, Path(project_dir).name, version_id)
    # The version is read for the frozen stages validation needs — which is also
    # what makes a guide for a version nobody stored fail here. It is only read:
    # the guide is its own document, so writing one leaves the snapshot untouched.
    version = load_version(project_dir, version_id)
    validate_review_guide(guide, version.stages)
    guide.save()
    return guide


def find_latest_review_guide(project: str, version_id: str) -> ReviewGuide | None:
    """The newest guide written for one version, or None if none was ever written."""
    # An autogenerated id carries no project prefix, so the backpointers are matched
    # in full rather than narrowed by the store first: this reads every stored guide.
    guides = [
        g for g in ReviewGuide.list()
        if g.project == project and g.version_id == version_id
    ]
    guides.sort(key=lambda g: (g.created_at, g.id), reverse=True)
    return guides[0] if guides else None


def validate_review_guide(guide: ReviewGuide, stages: list[Stage]) -> None:
    """Raises ReviewGuideValidationError naming every offending stage id and section."""
    refusals = [
        *_describe_stage_mismatch(guide, stages),
        *_describe_unnarrated_stages_reaching_publish(guide, stages),
        *_describe_sections_missing_data_description(guide),
    ]
    if refusals:
        raise ReviewGuideValidationError(" ".join(refusals))


def _describe_unnarrated_stages_reaching_publish(
    guide: ReviewGuide, stages: list[Stage]
) -> list[str]:
    hidden = sorted(set(guide.unnarrated) & find_stages_reaching_publish(stages))
    if not hidden:
        return []
    return [
        f"stage(s) listed unnarrated whose output reaches a publish stage: {hidden} — "
        "each one's work is carried into the published files, so a reader checking a "
        "published figure may have to check it, and leaving it out of the walkthrough "
        "hides it. Narrate each in a section. `unnarrated` is only for a stage that "
        "reaches NO publish stage."
    ]


def _describe_stage_mismatch(guide: ReviewGuide, stages: list[Stage]) -> list[str]:
    issues = _find_review_guide_issues(guide, stages)
    if not issues:
        return []
    return ["review guide does not match the version's stages: " + "; ".join(issues)]


def _describe_sections_missing_data_description(guide: ReviewGuide) -> list[str]:
    # Blank counts as absent; nothing is filled in from a title or a stage name.
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
    guide: ReviewGuide, project: str, version_id: str
) -> None:
    """Raises ValueError unless the guide's backpointers name the version being saved for."""
    if guide.project != project or guide.version_id != version_id:
        raise ValueError(
            f"guide describes {guide.project}/{guide.version_id}, not the version it is "
            f"being saved for: {project}/{version_id}"
        )


def _find_review_guide_issues(guide: ReviewGuide, stages: list[Stage]) -> list[str]:
    """All the ways `guide` misaccounts for `stages`, not the first — one rejection, whole story."""
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
    """A stored version document no longer validates — store corruption, or a
    version written under older model rules (e.g. a repo-relative path from
    before absolute paths were enforced). One error type meaning "this workflow
    doesn't validate", raised LOUDLY wherever the document is read: never a
    silent skip, which would make the version invisible while its id still
    occupies the store. The fix is a store migration/cleanup, not tolerance."""
    return WorkflowLoadError(f"version document {doc_id}", format_errors(exc))


def list_versions(project_dir: Path) -> list[WorkflowVersion]:
    """Prefer list_project_versions: this reads `project_dir` only to take its name."""
    return list_project_versions(Path(project_dir).name)


def list_project_versions(project_id: str) -> list[WorkflowVersion]:
    """NEWEST-FIRST. One invalid stored document fails the whole listing, loudly."""
    name = project_id
    versions: list[WorkflowVersion] = []
    for doc_id, data in get_store().read_all("workflow_version", f"{name}/"):
        try:
            v = WorkflowVersion.model_validate(data)
        except ValidationError as exc:
            raise _invalid_version_document(doc_id, exc) from exc
        versions.append(v)
    # version ids are strftime timestamps, so a reverse string sort on version_id
    # is chronological.
    versions.sort(key=lambda v: v.version_id, reverse=True)
    return versions


def find_latest_version_id(project_dir: Path) -> str | None:
    """The newest stored version's id, or None when the project stores none."""
    versions = list_versions(project_dir)  # newest-first
    return versions[0].version_id if versions else None


def resolve_version_id(project_dir: Path, version_id: str | None) -> str:
    """The version a run pins to; None means the newest stored one."""
    # A run is read-only with respect to versions: it never blanks the id, never
    # fabricates one, never silently reads the working copy, and never CREATES a
    # version as a side effect. Callers compose resolve_version_id ->
    # load_version_stages -> run, so the runner is handed a resolved snapshot and
    # reads no versions itself. Publication is a human review signal and is not
    # read here: any stored version runs.
    if version_id is not None:
        # load_version fails loudly on a missing version, and on one whose stored
        # document no longer validates — a caller asking for a specific id must
        # not be silently redirected to some other snapshot.
        load_version(project_dir, version_id)
        return version_id

    latest = find_latest_version_id(project_dir)
    if latest is None:
        # Never immortalise the working copy as a version to have something to run —
        # that is what let an invalid working copy poison "the latest" and fail every
        # subsequent run.
        raise NoVersionToRunError(
            f"No version to run for '{project_dir.name}'. A run executes a stored "
            f"version and never creates one — save a version first."
        )
    return latest


def validate_version_exists(project_dir: Path, version_id: str) -> None:
    """Raise FileNotFoundError unless this project stores this version id. Existence
    only — unlike load_version it reads no document body, so a stored version that no
    longer validates still passes."""
    name = Path(project_dir).name
    if not WorkflowVersion.exists(f"{name}/{version_id}"):
        raise FileNotFoundError(f"No version '{version_id}' for project '{name}'")


def load_version(project_dir: Path, version_id: str) -> WorkflowVersion:
    """This version, in full. Fails loudly if no such version is stored, or
    if the stored document no longer validates (WorkflowLoadError)."""
    name = Path(project_dir).name
    try:
        v = WorkflowVersion.load(f"{name}/{version_id}")
    except DocumentNotFound as exc:
        raise FileNotFoundError(f"No version '{version_id}' for project '{name}'") from exc
    except ValidationError as exc:
        raise _invalid_version_document(f"{name}/{version_id}", exc) from exc
    return v


def load_version_stages(project_dir: Path, version_id: str) -> list[Stage]:
    """This version's frozen stages, as typed Stage objects — already valid
    (embedded from a strict load at creation time), so WorkflowVersion.load's pydantic
    validation is the on-read integrity check and no re-load through the
    working-copy loader is needed. Fails loudly if the version is missing rather
    than falling back to the working copy (a run pinned to a version must read
    THAT version)."""
    name = Path(project_dir).name
    try:
        v = WorkflowVersion.load(f"{name}/{version_id}")
    except DocumentNotFound as exc:
        raise FileNotFoundError(f"No version '{version_id}' for project '{name}'") from exc
    except ValidationError as exc:
        raise _invalid_version_document(f"{name}/{version_id}", exc) from exc
    return v.stages

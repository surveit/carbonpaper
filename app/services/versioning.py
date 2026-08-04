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
from app.models import Coverage, Stage
from app.models.review_guide import ReviewGuideStep
from app.models.workflow import parse_workflow
from app.core.persistence import JsonDict, PersistedModel, PersistenceScope, get_store
from app.core.utils import format_errors
from app.services import node_review
from app.services.spec_migrations import (
    STAGE_SPEC_SCHEMA_VERSION,
    upgrade_stage_spec,
)
from app.services.errors import WorkflowLoadError
from app.services.loader import stage_to_spec_dict
from app.services.workspace import load_schemas


def _no_coverage() -> Coverage:
    # The zero-stage shape coverage_for itself returns for an empty stage list —
    # a WorkflowVersion constructed without an explicit `coverage=` (every
    # in-repo case is a test seeding a version directly) is born carrying it.
    return Coverage(approved=0, rejected=0, edited_stale=0, unreviewed=0,
                     total=0, approved_pct=0.0)


class WorkflowVersion(PersistedModel):
    """One frozen snapshot, stored in the "workflow_version" collection. `id` (inherited
    from PersistedModel) is the composite `f"{project}/{version_id}"`; `version_id`
    is the plain local id every caller of this module's public functions
    works with. `stages` and `schemas` are the frozen artifacts; `coverage` is
    approval coverage computed against `stages` at creation time. `published`
    (plus `published_at`/`published_by`) records the approval act that makes a
    version runnable — see the module docstring."""

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
    coverage: Coverage = Field(default_factory=_no_coverage)
    stages: list[Stage] = Field(default_factory=list)
    schemas: list[dict[str, Any]] = Field(default_factory=list)
    published: bool = False
    published_at: str | None = None
    published_by: str | None = None

    @classmethod
    def _upgrade(cls, data: JsonDict) -> JsonDict:
        """In-memory only, and never `schemas` — the data model keeps its keys."""
        for spec in data.get("stages") or []:
            if isinstance(spec, dict):
                upgrade_stage_spec(spec)
        return data


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
    (raw spec dicts) as a whole Workflow, embed the project's CURRENT schemas,
    freeze approval coverage against the live node_decisions store, and save —
    born unpublished. Returns the saved WorkflowVersion.

    `stages` is parsed via app.models.workflow.parse_workflow, which raises
    pydantic.ValidationError (per-stage schema errors AND cross-stage graph
    issues alike) on anything invalid; nothing is written in that case. Every
    version is therefore a loadable workflow, from this seam or any other.

    Coverage is computed from the SNAPSHOT's stages against the live
    node_decisions store, so the recorded coverage is exactly what was believed
    about these specs at this instant. schemas/ is read via
    workspace.load_schemas, which returns [] when the project has no schema
    library yet — a project with no data model still versions cleanly (the
    absence is truthful, not an error).

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

    # Freeze coverage from the just-snapshotted stages against the live
    # node_decisions store.
    spec_dicts = [stage_to_spec_dict(s) for s in workflow.stages]
    decisions = node_review.load_node_decisions(project_dir)
    coverage = Coverage.model_validate(node_review.coverage_for(spec_dicts, decisions))

    v = WorkflowVersion(
        id=doc_id,
        version_id=version_id,
        parent_version=parent_version,
        message=message,
        reviewer=reviewer,
        coverage=coverage,
        stages=workflow.stages,
        schemas=schemas,
        published=False,
    )
    v.save()
    return v


def publish_version(project_dir: Path, version_id: str, *, reviewer: str) -> WorkflowVersion:
    """Mark a version published: the metadata-only act that makes it eligible to
    run (see resolve_version_id below). Idempotent — publishing an
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
    """Raises ReviewGuideValidationError naming every offending stage id."""
    issues = _find_review_guide_issues(guide, stages)
    if issues:
        raise ReviewGuideValidationError(
            "review guide does not match the version's stages: " + "; ".join(issues)
        )


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
    """All versions for a project, NEWEST-FIRST. A stored document that fails
    the WorkflowVersion contract raises WorkflowLoadError (see
    _invalid_version_document) — the whole listing fails rather than quietly
    presenting a store with an invalid document in it as healthy. No versions
    stored yet -> []."""
    name = Path(project_dir).name
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
    """The newest stored version's id whatever its published state, or None when the
    project has no version yet. Not a substitute for resolve_version_id, which a
    production run uses because it gates on publication."""
    versions = list_versions(project_dir)  # newest-first
    return versions[0].version_id if versions else None


def resolve_version_id(project_dir: Path, version_id: str | None) -> str:
    """The PUBLISHED version a run pins to; None means the newest published one."""
    # A run is read-only with respect to versions: it never blanks the id, never
    # fabricates one, never silently reads the working copy, and never CREATES a
    # version as a side effect. Callers compose resolve_version_id ->
    # load_version_stages -> run, so the runner is handed a resolved snapshot and
    # reads no versions itself.
    if version_id is not None:
        # load_version fails loudly on a missing version — a caller asking for a
        # specific id must not be silently redirected to some other snapshot, and
        # an unreviewed draft must not run just because it was named.
        version = load_version(project_dir, version_id)
        if not version.published:
            raise NoVersionToRunError(
                f"Version '{version_id}' of '{project_dir.name}' is not published. "
                f"A run pins a published version — publish it first."
            )
        return version_id

    # Newest PUBLISHED, so a more recent unpublished version is skipped rather
    # than run unreviewed.
    for version in list_versions(project_dir):  # newest-first
        if version.published:
            return version.version_id

    # Never immortalise the working copy as a version to have something to run —
    # that is what let an invalid working copy poison "the latest" and fail every
    # subsequent run.
    raise NoVersionToRunError(
        f"No published version to run for '{project_dir.name}'. A run "
        f"targets a published version and never creates one — save a version "
        f"and publish it first."
    )


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


__all__ = [
    "WorkflowVersion",
    "ReviewGuide",
    "list_versions",
    "find_latest_version_id",
    "resolve_version_id",
    "validate_version_exists",
    "load_version",
    "load_version_stages",
    "create_version_from_stages",
    "publish_version",
    "save_version_guide",
    "find_latest_review_guide",
    "validate_review_guide",
]

"""
versioning.py — immutable, committable snapshots of a workflow.

A "version" is a `WorkflowVersion` document in the store's "workflow_version" collection: a frozen
copy of a project's authored artifacts — its compiled stages (typed, embedded
verbatim) and its schemas/ data model (embedded raw) — taken at a point in time,
plus who created it, why, its parent, and the approval coverage AT creation time.
Runs are pinned to a version and read its embedded stages, so a run is
reproducible against the exact workflow it executed, never "whatever the working
copy happened to be".

Each version's document id is `f"{project_dir.name}/{version_id}"` — project-scoped,
like every other collection in the store — so listing or loading against a project
with no versions yet returns empty results rather than requiring any scaffolding to
exist first. `version_id` uses the SAME timestamp scheme as run ids
(datetime.now().strftime('%Y%m%dT%H%M%S')) so versions and runs sort and read
consistently; it is the local "id" every caller of this module's public functions
works with — never the composite store id.

A version is born UNPUBLISHED (`published=False`): creating a snapshot and
approving it for runs are separate acts. `publish_version` is the only way a
version becomes published; a run (see app.runtime.runner.resolve_version_id)
refuses to target an unpublished version. A version document stored before the
`published` field existed carries no such key — it was created only by the
human "Create version" act publishing now records, so it reads as published
(see WorkflowVersion._grandfather_published).

`create_version_from_stages` is the ONE place a WorkflowVersion is written:
it strict-parses the given stage dicts, embeds the project's current schemas,
freezes approval coverage, and saves — nothing is written on a validation
failure. `create_version` (snapshot the working copy) is a thin adapter over
it: it strict-loads compiled/ into stage dicts and delegates.

Dependency note: this module may import app.services.node_review (to freeze
coverage), app.services.workspace (to read the working data model), and
app.core.models, but nothing from app.runtime or app.compiler. A version's stages
are parsed through the same strict parser as the working copy
(app.core.models.workflow.parse_workflow / app.services.loader), so a version's
stages load identically to the working copy's at the moment it was snapshotted.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from pydantic import Field, ValidationError, model_validator

from app.core.errors import DocumentNotFound
from app.core.models import Stage
from app.core.models.schema import format_errors
from app.core.models.workflow import parse_workflow
from app.core.persistence import PersistedModel, get_store
from app.services import node_review
from app.services.loader import WorkflowLoadError, load_workflow, stage_to_spec_dict
from app.services.workspace import load_schemas


class WorkflowVersion(PersistedModel):
    """One frozen snapshot, stored in the "workflow_version" collection. `id` (inherited
    from PersistedModel) is the composite `f"{project}/{version_id}"`; `version_id`
    is the plain local id every caller of this module's public functions
    works with. `stages` and `schemas` are the frozen artifacts; `coverage` is
    approval coverage computed against `stages` at creation time. `published`
    (plus `published_at`/`published_by`) records the approval act that makes a
    version runnable — see the module docstring."""

    collection: ClassVar[str] = "workflow_version"
    # Dump the embedded stages in their canonical spec-dict shape (field aliases
    # restored, unset optionals dropped) — the same convention stage_to_spec_dict
    # uses, so a version's on-disk stage shape matches the working copy's.
    DUMP_OPTS: ClassVar[dict[str, Any]] = {"by_alias": True, "exclude_none": True}

    version_id: str
    created_at: str
    parent_version: str | None = None
    message: str
    reviewer: str
    coverage: dict[str, Any] = Field(default_factory=dict)
    stages: list[Stage] = Field(default_factory=list)
    schemas: list[dict[str, Any]] = Field(default_factory=list)
    # This default never applies on load: `_grandfather_published` below injects
    # `True` whenever the input data carries no `published` key at all. Every
    # construction site MUST therefore pass `published=` explicitly — a site
    # that omits it is born published.
    published: bool = False
    published_at: str | None = None
    published_by: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _grandfather_published(cls, data: Any) -> Any:
        # A version record written before the `published` field existed was created
        # only by the human "Create version" act — the approval publishing now
        # records — so a missing key reads as published. New records always carry it.
        if isinstance(data, dict) and "published" not in data:
            return {**data, "published": True}
        return data


def _meta(v: WorkflowVersion) -> dict[str, Any]:
    """The meta dict shape every caller of this module reads: `id` is the LOCAL
    version_id, never the composite store id."""
    return {
        "id": v.version_id,
        "created_at": v.created_at,
        "parent_version": v.parent_version,
        "message": v.message,
        "reviewer": v.reviewer,
        "coverage": v.coverage,
        "published": v.published,
        "published_at": v.published_at,
        "published_by": v.published_by,
    }


def create_version_from_stages(
    project_dir: Path,
    stages: list[dict[str, Any]],
    *,
    message: str,
    reviewer: str,
    parent_version: str | None = None,
) -> dict[str, Any]:
    """The single write chokepoint for a WorkflowVersion: strict-parse `stages`
    (raw spec dicts) as a whole Workflow, embed the project's CURRENT schemas,
    freeze approval coverage against the live node_decisions store, and save —
    born unpublished. Returns its meta dict.

    `stages` is parsed via app.core.models.workflow.parse_workflow, which raises
    pydantic.ValidationError (per-stage schema errors AND cross-stage graph
    issues alike) on anything invalid; nothing is written in that case. Every
    version is therefore a loadable workflow, from this seam or any other.

    Coverage is computed from the SNAPSHOT's stages against the live
    node_decisions store, so the recorded coverage is exactly what was believed
    about these specs at this instant. schemas/ is read via
    workspace.load_schemas, which returns [] when the project has no schema
    library yet — a project with no data model still versions cleanly (the
    absence is truthful, not an error)."""
    project_dir = Path(project_dir)
    workflow = parse_workflow(stages)
    schemas = load_schemas(project_dir)

    version_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    project = project_dir.name
    doc_id = f"{project}/{version_id}"
    if WorkflowVersion.exists(doc_id):
        raise FileExistsError(
            f"Version already exists: {doc_id} (two versions created within one second)"
        )

    # Freeze coverage from the just-snapshotted stages against the live
    # node_decisions store.
    spec_dicts = [stage_to_spec_dict(s) for s in workflow.stages]
    decisions = node_review.load_node_decisions(project_dir)
    coverage = node_review.coverage_for(spec_dicts, decisions)

    v = WorkflowVersion(
        id=doc_id,
        version_id=version_id,
        created_at=datetime.now().isoformat(timespec="seconds"),
        parent_version=parent_version,
        message=message,
        reviewer=reviewer,
        coverage=coverage,
        stages=workflow.stages,
        schemas=schemas,
        published=False,
    )
    v.save()
    return _meta(v)


def create_version(
    project_dir: Path,
    *,
    message: str,
    reviewer: str,
    parent_version: str | None = None,
) -> dict[str, Any]:
    """Snapshot the working copy's compiled stages + schemas into a new Version
    document. Returns its meta dict. A thin adapter over
    create_version_from_stages: the working copy is strict-loaded first,
    through the same loader the runner uses (WorkflowLoadError, saving
    nothing, if it is not a valid workflow), then handed to
    create_version_from_stages as spec dicts — the single write chokepoint."""
    project_dir = Path(project_dir)
    compiled_src = project_dir / "compiled"
    if not compiled_src.is_dir():
        raise FileNotFoundError(
            f"Cannot create a version: no compiled/ workflow at {compiled_src}"
        )

    # Validate BEFORE writing anything: a version is, by invariant, a loadable
    # workflow. On failure load_workflow raises WorkflowLoadError and we save
    # nothing — an invalid workflow can never be immortalised as a version. (The
    # run-path strict load then only guards on-read corruption of an
    # already-valid snapshot.)
    stages = load_workflow(project_dir)
    return create_version_from_stages(
        project_dir,
        [stage_to_spec_dict(s) for s in stages],
        message=message,
        reviewer=reviewer,
        parent_version=parent_version,
    )


def publish_version(project_dir: Path, version_id: str, *, reviewer: str) -> dict[str, Any]:
    """Mark a version published: the metadata-only act that makes it eligible to
    run (see app.runtime.runner.resolve_version_id). Idempotent — publishing an
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
        return _meta(v)
    v.published = True
    v.published_at = datetime.now().isoformat(timespec="seconds")
    v.published_by = reviewer
    v.save()
    return _meta(v)


def _invalid_version_document(doc_id: str, exc: ValidationError) -> WorkflowLoadError:
    """A stored version document no longer validates — store corruption, or a
    version written under older model rules (e.g. a repo-relative path from
    before absolute paths were enforced). One error type meaning "this workflow
    doesn't validate", raised LOUDLY wherever the document is read: never a
    silent skip, which would make the version invisible while its id still
    occupies the store. The fix is a store migration/cleanup, not tolerance."""
    return WorkflowLoadError(f"version document {doc_id}", format_errors(exc))


def list_versions(project_dir: Path) -> list[dict[str, Any]]:
    """All versions for a project, NEWEST-FIRST, each as its meta dict. A stored
    document that fails the WorkflowVersion contract raises WorkflowLoadError
    (see _invalid_version_document) — the whole listing fails rather than
    quietly presenting a store with an invalid document in it as healthy.
    No versions stored yet -> []."""
    name = Path(project_dir).name
    metas: list[dict[str, Any]] = []
    for doc_id, data in get_store().read_all("workflow_version", f"{name}/"):
        try:
            v = WorkflowVersion.model_validate(data)
        except ValidationError as exc:
            raise _invalid_version_document(doc_id, exc) from exc
        metas.append(_meta(v))
    # version ids are strftime timestamps, so a reverse string sort on id is
    # chronological.
    metas.sort(key=lambda m: str(m["id"]), reverse=True)
    return metas


def load_version_meta(project_dir: Path, version_id: str) -> dict[str, Any]:
    """This version's meta dict. Fails loudly if no such version is stored, or
    if the stored document no longer validates (WorkflowLoadError)."""
    name = Path(project_dir).name
    try:
        v = WorkflowVersion.load(f"{name}/{version_id}")
    except DocumentNotFound as exc:
        raise FileNotFoundError(f"No version '{version_id}' for project '{name}'") from exc
    except ValidationError as exc:
        raise _invalid_version_document(f"{name}/{version_id}", exc) from exc
    return _meta(v)


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
    "list_versions",
    "load_version_meta",
    "load_version_stages",
    "create_version",
    "create_version_from_stages",
    "publish_version",
]

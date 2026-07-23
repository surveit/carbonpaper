"""drafts.py — the DRAFT lifecycle: disposable, mutable scratch space for a
workflow's stages.

A draft is where an agent (or, later, a UI edit buffer) assembles stages
before freezing them into an immutable version. Unlike a version it is
mutable and carries no promise of survival: a `Draft` is a document in the
store's "draft" collection, doc id `f"{project}/{draft_id}"` — project-scoped
like every other collection — kept purely so an in-flight edit survives a
server restart. Anything may delete a draft at any time, nothing may depend on
one existing, and drafts are never project state (not versioned, not run).

Every stored stage is a valid `Stage` — set_draft_stage rejects a malformed
one outright (see its docstring). What stays allowed mid-edit is WORKFLOW-level
incompleteness: a valid stage whose `inputs` reference a stage id not yet in
the draft, a duplicate id, or a cycle — the cross-stage graph checks
(`app.models.workflow.validate_workflow`) — since a draft is a workflow
under construction, not yet a finished one. The only exit is save_version,
which requires the whole graph to be clean and refuses rather than persist an
incomplete workflow.

Draft ids are word triplets (e.g. brisk-otter-lamp): short enough for an agent
to retype reliably, and unmistakable for a timestamp version id — see
app.core.utils.generate_word_triplet_id.

Dependency note: this module may import app.services.versioning (to seed from
and freeze into a version) and app.services.workspace (to resolve a project
name to its directory), and app.core.*, but nothing from app.runtime or
app.compiler."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field, ValidationError

from app.core.errors import DocumentNotFound, DraftNotFoundError
from app.models import Stage, validate_workflow
from app.core.persistence import PersistedModel, PersistenceScope
from app.core.utils import format_errors, generate_word_triplet_id
from app.services import versioning, workspace
from app.services.loader import stage_to_spec_dict


class Draft(PersistedModel):
    """One scratch document in the "draft" collection. `id` (inherited from
    PersistedModel) is the composite `f"{project}/{draft_id}"`; `draft_id` is
    the plain local id every caller of this module's public functions works
    with. `stages` are validated `Stage` objects — each one individually
    valid — but the WORKFLOW they form may still be incomplete mid-edit (a
    dangling input, a duplicate id, a cycle) until save_version."""

    collection: ClassVar[str] = "draft"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.AUTHORED
    # Dump embedded stages in their canonical spec-dict shape (field aliases
    # restored, unset optionals dropped) — mirrors WorkflowVersion.DUMP_OPTS,
    # so a draft's on-disk stage shape matches a version's.
    DUMP_OPTS: ClassVar[dict[str, Any]] = {"by_alias": True, "exclude_none": True}

    draft_id: str
    parent_version: str | None = None
    stages: list[Stage] = Field(default_factory=list)


class DraftView(BaseModel):
    """The agent-facing shape every caller of this module reads: `id` is the
    LOCAL draft_id, never the composite store id. `stages` are validated
    `Stage` objects — see `Draft`'s docstring for what "valid" does and
    doesn't cover mid-edit."""

    id: str
    parent_version: str | None
    stages: list[Stage]
    created_at: str
    updated_at: str


class DraftDetail(DraftView):
    """A draft's view plus its current validation problems."""

    issues: list[str]


class DraftEdit(BaseModel):
    """The summary every draft edit (set/remove stage) returns: what's in the
    draft now, and whether it would save cleanly."""

    ok: bool
    draft_id: str
    stage_ids: list[str]
    issues: list[str]


class SaveResult(BaseModel):
    """The outcome of freezing a draft into a version: either refused with the
    blocking `issues` (nothing written, `version_id` stays None), or the new
    version's id."""

    ok: bool
    issues: list[str] = Field(default_factory=list)
    version_id: str | None = None


def _view(d: Draft) -> DraftView:
    """Project a Draft down to its public view."""
    return DraftView(
        id=d.draft_id,
        parent_version=d.parent_version,
        stages=d.stages,
        created_at=d.created_at,
        updated_at=d.updated_at,
    )


def create_draft(
    name: str,
    *,
    from_version: str | None = None,
    examples_dir: Path | None = None,
) -> DraftView:
    """Start a new draft for project `name` and return its view (its `id` names
    it in every later call). Seeded with the stages of `from_version` when
    given (and recording it as the draft's parent), empty otherwise."""
    project_dir = workspace.resolve_project_dir(name, examples_dir)
    stages = (
        versioning.load_version_stages(project_dir, from_version)
        if from_version is not None
        else []
    )
    draft_id = generate_word_triplet_id(_taken(project_dir))
    d = Draft(
        id=_doc_id(project_dir, draft_id),
        draft_id=draft_id,
        parent_version=from_version,
        stages=stages,
    )
    d.save()
    return _view(d)


def read_draft(
    name: str, draft_id: str, *, examples_dir: Path | None = None
) -> DraftDetail:
    """The draft's view plus a non-fatal `issues` list (the cross-stage graph
    problems in its current stages — dangling inputs, duplicate ids, a cycle;
    [] means it would save cleanly). Every stored stage is already individually
    valid, so nothing here re-checks a single stage's own shape."""
    project_dir = workspace.resolve_project_dir(name, examples_dir)
    d = _load(project_dir, draft_id)
    return DraftDetail(**_view(d).model_dump(), issues=validate_workflow(d.stages))


def set_draft_stage(
    name: str, draft_id: str, stage_json: str, *, examples_dir: Path | None = None
) -> DraftEdit:
    """Add or replace one stage (matched by its `id`) in the draft. A MALFORMED
    stage — invalid JSON, not a JSON object, or failing `Stage` validation
    (unknown type, missing required field, wrong shape, ...) — is rejected
    outright: nothing is written, and `ValueError` carries the readable
    per-field errors so the caller fixes and retries. A VALID stage whose
    `inputs` reference a stage id not yet in the draft IS stored — that's the
    workflow still being incomplete mid-build, not a bad stage — and shows up
    in the returned `issues`."""
    stage = _parse_stage(stage_json)
    project_dir = workspace.resolve_project_dir(name, examples_dir)
    d = _load(project_dir, draft_id)
    kept = [s for s in d.stages if s.id != stage.id]
    d.stages = kept + [stage]
    d.save()
    return _describe(d)


def remove_draft_stage(
    name: str, draft_id: str, stage_id: str, *, examples_dir: Path | None = None
) -> DraftEdit:
    """Delete one stage from the draft by id. Raises ValueError if no stage in
    the draft carries that id (deleting nothing is a caller mistake, not a
    success)."""
    project_dir = workspace.resolve_project_dir(name, examples_dir)
    d = _load(project_dir, draft_id)
    kept = [s for s in d.stages if s.id != stage_id]
    if len(kept) == len(d.stages):
        raise ValueError(f"No stage '{stage_id}' in draft '{draft_id}'")
    d.stages = kept
    d.save()
    return _describe(d)


def save_version(
    name: str, draft_id: str, *, message: str, examples_dir: Path | None = None
) -> SaveResult:
    """Freeze the draft's stages into a new immutable version — the draft's only
    exit, and the single validation cliff: a workflow still incomplete (a
    dangling input, a duplicate id, a cycle) is refused with the full issue
    list and nothing is written. On success the draft's parent advances to the
    new version, so successive saves chain (v2 -> v3 -> v4) rather than
    fanning out; the version is born unpublished (publishing is the human's
    act)."""
    project_dir = workspace.resolve_project_dir(name, examples_dir)
    d = _load(project_dir, draft_id)
    issues = validate_workflow(d.stages)
    if issues:
        return SaveResult(ok=False, issues=issues)
    meta = versioning.create_version_from_stages(
        project_dir,
        [stage_to_spec_dict(s) for s in d.stages],
        message=message,
        reviewer="agent",
        parent_version=d.parent_version,
    )
    d.parent_version = meta.version_id
    d.save()
    return SaveResult(ok=True, version_id=meta.version_id)


# ─── internals ───────────────────────────────────────────────────────────────

_DRAFT_ID = re.compile(r"^[a-z]+-[a-z]+-[a-z]+$")


def _doc_id(project_dir: Path, draft_id: str) -> str:
    return f"{Path(project_dir).name}/{draft_id}"


def _taken(project_dir: Path) -> set[str]:
    """The local draft ids already live for this project — the space
    generate_word_triplet_id must avoid."""
    return {d.draft_id for d in Draft.list(f"{Path(project_dir).name}/")}


def _load(project_dir: Path, draft_id: str) -> Draft:
    """The draft, with the id checked against the triplet shape FIRST so a
    caller-supplied id can never be used as a store key it wasn't minted from
    (e.g. a path-traversal attempt), then loaded from the store. A missing
    document reads the same as a malformed id — both mean "no such draft"."""
    if not _DRAFT_ID.match(draft_id):
        raise DraftNotFoundError(f"'{draft_id}' is not a draft id")
    try:
        return Draft.load(_doc_id(project_dir, draft_id))
    except DocumentNotFound as exc:
        raise DraftNotFoundError(
            f"No draft '{draft_id}' for project '{Path(project_dir).name}' — drafts are "
            f"disposable; start a new one with create_draft."
        ) from exc


def _parse_stage(stage_json: str) -> Stage:
    """Parse `stage_json` as one `Stage`, raising `ValueError` (with readable
    per-field errors) for anything MALFORMED: invalid JSON, not a JSON object,
    or failing `Stage.model_validate`. `Stage` validation is per-stage only —
    it does not check whether `inputs` reference a stage id that exists
    elsewhere in the draft, which is a cross-stage graph concern (see
    app.models.workflow.check_inputs_resolve) and stays allowed here."""
    obj = json.loads(stage_json)
    if not isinstance(obj, dict):
        raise ValueError("stage_json must be a JSON object")
    try:
        return Stage.model_validate(obj)
    except ValidationError as exc:
        raise ValueError("; ".join(format_errors(exc))) from exc


def _describe(d: Draft) -> DraftEdit:
    """The summary every edit returns: what's in the draft now, and whether it
    would save cleanly."""
    return DraftEdit(
        ok=True,
        draft_id=d.draft_id,
        stage_ids=[s.id for s in d.stages],
        issues=validate_workflow(d.stages),
    )

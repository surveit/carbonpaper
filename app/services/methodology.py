"""
methodology.py — adjudicated stage-test disputes flow back into the methodology
prose as reviewable AMENDMENTS.

Context (issues #149-#153): stage tests pin down a python transform's behavior.
N-version differential derivation (#152) can surface an *ambiguity* — a case the
frozen tests leave underdetermined — which the user ADJUDICATES: they pick the
intended semantic, or edit a test to encode it. Left there, that resolution would
live only in the test set, making the tests a SHADOW SPEC (issue #153). This
module keeps the PROSE canonical.

An adjudication is captured as an `Amendment`: a proposed prose section recording
WHAT was ambiguous and WHAT was decided, attributed to a reviewer and timestamped.
Amendments are NOT silently merged into the document — they are proposed, then
explicitly PUBLISHED, at which point their prose is appended to the project's
canonical methodology document with a provenance header. So the resolution ends up
in the prose the workflow is authored from, not hidden beside the code.

Lifecycle (mirrors the belief discipline used elsewhere — node_review approvals,
versioning snapshots): a change to canonical prose is deliberate and attributable,
never an anonymous mutation.

    proposed ──publish──▶ published   (prose appended to the methodology document)
             └─reject───▶ rejected    (recorded; the document is left untouched)

Storage: <project>/methodology_amendments/<amendment_id>.json — one JSON file per
amendment, git-diffable and human-readable (the versioning module's per-id record
convention, not a parquet: amendments are prose + provenance, low-volume, meant to
be read). `amendment_id` uses the SAME strftime scheme as version and run ids
(datetime.now().strftime('%Y%m%dT%H%M%S')) so amendments sort chronologically by
id like everything else.

Dependency note (mirrors the services discipline in AGENTS.md): this module imports
only stdlib, pydantic, and the sibling SERVICE app.services.project (to resolve the
canonical document through the one probe that owns that convention). It imports
NOTHING from app.runtime / app.compiler / app.web.

EXTENSION POINT (UI, deferred): the stage-tests adjudication surface (#149-#152) is
not on this branch yet. When it lands, its `AmbiguityFinding` (stage_id / stage_name
/ summary) maps directly onto `Adjudication` — the finding's fields become
`stage_id` / `stage_name` / `ambiguity`, and the user's chosen semantic (or edited
test) becomes `resolution` — and the UI calls `record_adjudication` at the moment
the dispute is resolved. Nothing here presumes that surface exists, so this seam can
be wired in without change.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel

from app.services import project

# How the user resolved the dispute: they picked the intended semantic outright,
# or they edited/added a test that encodes it (test_name then names that test).
ResolutionKind = Literal["picked_semantic", "edited_test"]

# An amendment's lifecycle state. `proposed` on creation; `published` once its prose
# has been appended to the canonical document; `rejected` if the reviewer discards it
# (the document is never touched in that case).
AmendmentStatus = Literal["proposed", "published", "rejected"]

# Marker anchoring a published amendment inside the methodology document. Carries the
# amendment id so a publish is traceable back to its record and idempotent (publishing
# never appends the same amendment's prose twice).
_AMENDMENT_MARKER = "<!-- methodology-amendment:{id} -->"


class Adjudication(BaseModel):
    """The decision a user made resolving a stage-test dispute: WHAT was ambiguous
    (`ambiguity`) and WHAT was decided (`resolution`).

    Maps directly onto the `AmbiguityFinding` a future differential-derivation UI
    (#152) hands over: its stage_id / stage_name / summary become `stage_id` /
    `stage_name` / `ambiguity`, and the user's chosen semantic (or the test they
    edited) becomes `resolution`. `resolution_kind` records HOW they decided, and
    `test_name` names the test when they encoded the semantic by editing one — so an
    amendment can point at the concrete test that now guards the resolved behavior."""

    stage_id: str
    stage_name: str
    ambiguity: str
    resolution: str
    resolution_kind: ResolutionKind = "picked_semantic"
    test_name: Optional[str] = None


class Amendment(BaseModel):
    """A proposed (or published / rejected) amendment to the methodology prose, born
    from one `adjudication`.

    `prose` is the rendered markdown section that flows into the document on publish.
    `reviewer` + `created_at` (+ `published_at`) attribute the change so a mutation of
    canonical prose is never anonymous. `document` records the file name the prose was
    (or will be) appended to — the project's canonical authored document at the time."""

    id: str
    created_at: str
    reviewer: str
    status: AmendmentStatus
    adjudication: Adjudication
    prose: str
    document: Optional[str] = None
    published_at: Optional[str] = None


def amendments_dir(project_dir: Path) -> Path:
    """<project>/methodology_amendments/ — the parent of all amendment records."""
    return Path(project_dir) / "methodology_amendments"


def render_amendment_prose(
    adjudication: Adjudication,
    *,
    amendment_id: str,
    reviewer: str,
    created_at: str,
) -> str:
    """Render one adjudication as a self-contained markdown amendment section.

    The section names the stage, attributes the decision (who / when / which
    amendment), then states the ambiguity and the resolution as prose — the canonical
    form the methodology should carry, so the resolved behavior is spec, not a test
    the reader has to reverse-engineer. When the user encoded the semantic by editing
    a test, the test is named so a reader can find the case that guards it."""
    date = created_at.split("T", 1)[0]
    lines = [
        f"## Methodology amendment — {adjudication.stage_name}",
        "",
        f"*Adjudicated by {reviewer} on {date}. Resolves a stage-test ambiguity for "
        f"stage `{adjudication.stage_id}` (amendment {amendment_id}).*",
        "",
        f"**Ambiguity.** {adjudication.ambiguity}",
        "",
        f"**Resolution.** {adjudication.resolution}",
    ]
    if adjudication.resolution_kind == "edited_test" and adjudication.test_name:
        lines += ["", f"This behavior is guarded by the test `{adjudication.test_name}`."]
    return "\n".join(lines)


def _amendment_path(project_dir: Path, amendment_id: str) -> Path:
    return amendments_dir(project_dir) / f"{amendment_id}.json"


def _write(project_dir: Path, amendment: Amendment) -> None:
    """Persist one amendment record as <project>/methodology_amendments/<id>.json."""
    adir = amendments_dir(project_dir)
    adir.mkdir(parents=True, exist_ok=True)
    (adir / f"{amendment.id}.json").write_text(
        json.dumps(amendment.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def record_adjudication(
    project_dir: Path,
    adjudication: Adjudication,
    *,
    reviewer: str = "local",
) -> Amendment:
    """Capture an adjudication decision as a PROPOSED methodology amendment and
    persist it. Returns the created Amendment (status `proposed`).

    This is the seam the adjudication UI (#149-#152) calls the instant a dispute is
    resolved: the decision is recorded as a reviewable proposal, NOT written into the
    canonical prose (that is `publish_amendment`'s deliberate, attributable step). The
    amendment records the project's current canonical document name so a later publish
    knows which file it targets; None when the project has no document yet (the
    proposal is still captured — the absence is truthful, and publish fails loudly)."""
    project_dir = Path(project_dir)
    amendment_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    created_at = datetime.now().isoformat(timespec="seconds")
    doc = project.document_path(project_dir)
    amendment = Amendment(
        id=amendment_id,
        created_at=created_at,
        reviewer=reviewer,
        status="proposed",
        adjudication=adjudication,
        prose=render_amendment_prose(
            adjudication,
            amendment_id=amendment_id,
            reviewer=reviewer,
            created_at=created_at,
        ),
        document=doc.name if doc is not None else None,
        published_at=None,
    )
    _write(project_dir, amendment)
    return amendment


def load_amendment(project_dir: Path, amendment_id: str) -> Amendment:
    """Load one amendment record. Raises FileNotFoundError if absent."""
    p = _amendment_path(Path(project_dir), amendment_id)
    if not p.exists():
        raise FileNotFoundError(f"No amendment '{amendment_id}' at {p}")
    return Amendment.model_validate_json(p.read_text(encoding="utf-8"))


def list_amendments(project_dir: Path) -> list[Amendment]:
    """All amendments for a project, NEWEST-FIRST (ids are strftime timestamps, so a
    reverse sort on id is chronological). Skips any file that does not parse as an
    Amendment rather than fabricating a record for it — a half-written file is simply
    not listed. Returns [] when the project has no amendments dir yet."""
    adir = amendments_dir(Path(project_dir))
    if not adir.is_dir():
        return []
    out: list[Amendment] = []
    for f in adir.glob("*.json"):
        try:
            out.append(Amendment.model_validate_json(f.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            continue
    out.sort(key=lambda a: a.id, reverse=True)
    return out


def publish_amendment(project_dir: Path, amendment_id: str) -> Amendment:
    """Flow a proposed amendment BACK INTO the canonical prose: append its rendered
    section (behind a traceable marker) to the project's authored methodology document,
    then mark the amendment `published` and stamp `published_at`. Returns the updated
    Amendment.

    Keeping prose canonical is the whole point of #153 — so this is the one place the
    document is mutated, and it is deliberate, attributed (the prose carries reviewer +
    date), and reversible by editing the document. Fails loudly rather than guessing:

      - no authored document on disk  → FileNotFoundError (nothing to amend; the
        resolution stays a proposal, never invents a document);
      - amendment already `published` → ValueError (idempotent: a second publish is a
        caller bug, not a duplicated section — the marker would also catch it);
      - amendment `rejected`          → ValueError (a discarded decision must not reach
        the prose)."""
    project_dir = Path(project_dir)
    amendment = load_amendment(project_dir, amendment_id)

    if amendment.status == "published":
        raise ValueError(f"amendment '{amendment_id}' is already published")
    if amendment.status == "rejected":
        raise ValueError(f"amendment '{amendment_id}' was rejected; cannot publish it")

    doc = project.document_path(project_dir)
    if doc is None:
        raise FileNotFoundError(
            f"Cannot publish amendment '{amendment_id}': project at {project_dir} "
            f"has no methodology document to amend"
        )

    marker = _AMENDMENT_MARKER.format(id=amendment_id)
    existing = doc.read_text(encoding="utf-8")
    # Idempotency backstop: if the marker is somehow already in the document (e.g. a
    # record went stale), do not append a second copy — just reconcile the record.
    if marker not in existing:
        block = f"\n\n{marker}\n{amendment.prose}\n"
        doc.write_text(existing + block, encoding="utf-8")

    published = amendment.model_copy(
        update={
            "status": "published",
            "published_at": datetime.now().isoformat(timespec="seconds"),
            "document": doc.name,
        }
    )
    _write(project_dir, published)
    return published


def reject_amendment(
    project_dir: Path, amendment_id: str, *, note: Optional[str] = None
) -> Amendment:
    """Discard a proposed amendment: mark it `rejected` (recording `note` in the prose
    trail is left to the caller's own log). The canonical document is NOT touched — a
    rejected adjudication never reaches the prose. Returns the updated Amendment.

    Raises ValueError if the amendment was already published (published prose lives in
    the document; unwinding it is a document edit, not a status flip)."""
    project_dir = Path(project_dir)
    amendment = load_amendment(project_dir, amendment_id)
    if amendment.status == "published":
        raise ValueError(
            f"amendment '{amendment_id}' is published; edit the document to unwind it"
        )
    updates: dict[str, object] = {"status": "rejected"}
    if note:
        updates["prose"] = f"{amendment.prose}\n\n<!-- rejected: {note} -->"
    rejected = amendment.model_copy(update=updates)
    _write(project_dir, rejected)
    return rejected


__all__ = [
    "ResolutionKind",
    "AmendmentStatus",
    "Adjudication",
    "Amendment",
    "amendments_dir",
    "render_amendment_prose",
    "record_adjudication",
    "load_amendment",
    "list_amendments",
    "publish_amendment",
    "reject_amendment",
]

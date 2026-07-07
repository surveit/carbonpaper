"""
project.py — the PROJECT MODEL: one status object per project working copy.

A "project" is a directory under examples/<name>/. Over its life it accumulates,
in order: a source DOCUMENT (the pasted methodology), a DATA MODEL (named
schemas/), a WORKFLOW (the compiled/ stages), immutable VERSIONS, and RUNS. The
unified app shows ONE project at a time with five sections (Overview / Document /
Data model / Workflow / Runs); every section route and the shell that frames them
need the same status snapshot. This module computes that snapshot —
`project_state(pdir)` — so the routes layer never recomputes counts ad hoc and
never disagrees with the sidebar about what's done and what to do next.

Two functions are the public surface:
  - project_meta(pdir)  : the project's identity card (name/title/created/model/
                          source). Reads examples/<name>/project.json when present;
                          for LEGACY projects with none, degrades TRUTHFULLY — never
                          invents a model or a creation date.
  - project_state(pdir) : the status object the Overview + shell render, including
                          the `next_action` ladder ("what to do next").
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.services import node_review, versioning
from app.web.loading import load_schemas


# ─── Document discovery ───────────────────────────────────────────────────────
# A project's source document is the pasted methodology it was authored from. The
# create flow writes document.md; legacy/imported projects may carry
# methodology_raw.md or the older methodology_raw.txt. Probe in that order and
# report the first that exists (a truthful path, never a fabricated one).
#
# LEGACY: probing a fixed candidate list is a migration accommodation. The intended
# direction is for project.json to record the document's path explicitly, so a
# project references a real file rather than inferring it by filename — at which
# point this probe (and _DOCUMENT_CANDIDATES) can be retired.
_DOCUMENT_CANDIDATES = ("document.md", "methodology_raw.md", "methodology_raw.txt")


def _document_path(pdir: Path) -> Path | None:
    """First existing source-document file in <pdir> (see _DOCUMENT_CANDIDATES),
    or None when the project has no document on disk. Returns the absolute Path so
    callers can read or link it; None is a truthful 'no document', not an error."""
    for name in _DOCUMENT_CANDIDATES:
        p = pdir / name
        if p.is_file():
            return p
    return None


# ─── Stage loading (counts / coverage) ────────────────────────────────────────


def _load_compiled_stages(pdir: Path) -> list[dict[str, Any]]:
    """Load the working copy's compiled/ stages as raw dicts, mirroring
    app.services.loader's on-disk convention (sorted glob of compiled/*.json,
    inject _filename/_order, surface a parse error as an _error stage rather than
    dropping it). Returns [] when there is no compiled/ workflow yet. Used for
    counting stages and computing approval coverage — so the count and coverage
    here match exactly what the workflow page loads."""
    compiled_dir = pdir / "compiled"
    if not compiled_dir.is_dir():
        return []
    stages: list[dict[str, Any]] = []
    for json_file in sorted(compiled_dir.glob("*.json")):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError as exc:
            data = {
                "id": json_file.stem,
                "name": f"[JSON ERROR] {json_file.name}",
                "type": "python_transform",
                "compiler_notes": [f"JSON parse error: {exc}"],
                "_error": True,
            }
        data["_filename"] = json_file.name
        data["_order"] = json_file.stem.split("_", 1)[0]
        stages.append(data)
    return stages


# ─── Run summary ──────────────────────────────────────────────────────────────


def _runs_summary(pdir: Path) -> dict[str, Any]:
    """Summarise the project's runs/ dir: {n, awaiting_review, latest_status}.

    Mirrors loading.list_runs exactly: a run is a child dir of runs/ WITH a readable
    manifest.json; dirs lacking one (partial / legacy-output-only) are not counted,
    so n is the count of real runs, never inflated. `awaiting_review` counts runs
    whose status is 'awaiting_review' (halted at a human_review_queue) — the driver
    of the "review the run" rung of the ladder. `latest_status` is the newest run's
    status (runs are timestamp-id'd, so the max id is newest); None when there are
    no runs. A corrupt manifest is counted (status 'corrupt') rather than hidden."""
    runs_dir = pdir / "runs"
    if not runs_dir.is_dir():
        return {"n": 0, "awaiting_review": 0, "latest_status": None}
    statuses: list[tuple[str, str]] = []  # (run_id, status)
    awaiting = 0
    for run in runs_dir.iterdir():
        if not run.is_dir():
            continue
        manifest_path = run / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            status = manifest.get("status", "unknown")
        except json.JSONDecodeError:
            status = "corrupt"
        statuses.append((run.name, status))
        if status == "awaiting_review":
            awaiting += 1
    if not statuses:
        return {"n": 0, "awaiting_review": 0, "latest_status": None}
    # Newest run by id (run ids are strftime timestamps → lexical max is chronological).
    latest_status = max(statuses, key=lambda t: t[0])[1]
    return {"n": len(statuses), "awaiting_review": awaiting, "latest_status": latest_status}


# ─── Project identity (meta) ──────────────────────────────────────────────────


def project_meta(pdir: Path) -> dict[str, Any]:
    """The project's identity card: {name, title, created_at, model, source}.

    Reads examples/<name>/project.json when present (the record a gated-authored
    project will carry). For a LEGACY project with no project.json, degrade
    TRUTHFULLY rather than fabricate:
      - name       : the directory name (always known).
      - title      : project.json's title, else None (no invented prose title).
      - created_at : project.json's value, else None. The create flow always writes
                     it, so a None here means a legacy project that predates it —
                     reported as unknown, never an inferred date.
      - model      : project.json's value, else None — "unknown". We do NOT guess a
                     default model; a wrong provenance is worse than an honest gap.
      - source     : project.json's value, else None.

    Always returns name from the dir even if project.json is malformed, so a corrupt
    file degrades to legacy behaviour instead of raising."""
    pdir = Path(pdir)
    name = pdir.name

    raw: dict[str, Any] = {}
    pj = pdir / "project.json"
    if pj.is_file():
        try:
            loaded = json.loads(pj.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (json.JSONDecodeError, OSError):
            raw = {}

    return {
        "name": raw.get("name") or name,
        "title": raw.get("title"),
        # project.json's value, else None — the create flow always sets it; a None is
        # an honest "unknown" for a legacy project, never an inferred date.
        "created_at": raw.get("created_at"),
        # model is None ("unknown") for legacy — never a fabricated default.
        "model": raw.get("model"),
        "source": raw.get("source"),
    }


def write_project_meta(pdir: Path, **fields: Any) -> dict[str, Any]:
    """Merge `fields` into examples/<name>/project.json and persist it, returning the
    written record. Reads any existing file first so a partial update doesn't drop
    other keys; only the keys passed are overwritten. None values ARE written when
    passed explicitly (so a caller can clear a field), but callers should omit a key
    rather than pass a placeholder — this module never invents a model/date itself."""
    pdir = Path(pdir)
    pdir.mkdir(parents=True, exist_ok=True)
    pj = pdir / "project.json"
    record: dict[str, Any] = {}
    if pj.is_file():
        try:
            loaded = json.loads(pj.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                record = loaded
        except (json.JSONDecodeError, OSError):
            record = {}
    record.update(fields)
    pj.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return record


# ─── The status snapshot + next-action ladder ─────────────────────────────────


def project_state(pdir: Path) -> dict[str, Any]:
    """The single status object the Overview page and the shell sidebar render.

    Shape:
      {
        name, meta,
        has_document, document_path,
        data_model: {present, n_schemas, state},
        workflow:   {present, n_stages, coverage|None},
        versions:   n,
        runs:       {n, awaiting_review, latest_status},
        next_action:{key, label, href},
      }

    Every field is read off disk truthfully:
      - data_model.state uses node_review.data_model_state over the LIVE schemas
        (approved | edited_stale | unreviewed); 'none' when there is no data model
        (we report the absence, we do not run the gate on an empty list).
      - workflow.coverage is node_review.coverage_for over the COMPILED stages
        (approved/total/…); None — not a zero object — when there is no workflow.
      - runs comes from _runs_summary (manifest-backed runs only).

    next_action is the "what to do next" ladder, evaluated top-down (first match
    wins). hrefs are section paths under /project/<name> (the unified routes):
      1. no data model            → author it     (/project/<id>/data_model)
      2. data model not approved   → approve it     (/project/<id>/data_model)
      3. no workflow              → build the workflow (/project/<id>/workflow)
      4. workflow approved<total   → review the workflow (/project/<id>/workflow)
      5. workflow approved, 0 runs → run it          (/project/<id>/workflow)
      6. runs awaiting_review>0    → review the run   (/project/<id>/runs)
      7. otherwise                → view runs        (/project/<id>/runs)
    """
    pdir = Path(pdir)
    name = pdir.name
    meta = project_meta(pdir)

    # ── Document ──
    doc_path = _document_path(pdir)
    has_document = doc_path is not None

    # ── Data model (named schemas) ──
    schemas = load_schemas(pdir)
    dm_present = bool(schemas)
    if dm_present:
        dm_state = node_review.data_model_state(pdir, schemas)["state"]
    else:
        # No data model authored yet — report the absence; do NOT run the gate over
        # an empty schema set (that would manufacture an 'unreviewed' verdict for a
        # thing that doesn't exist).
        dm_state = "none"
    data_model = {"present": dm_present, "n_schemas": len(schemas), "state": dm_state}

    # ── Workflow (compiled stages) ──
    stages = _load_compiled_stages(pdir)
    wf_present = bool(stages)
    if wf_present:
        decisions = node_review.load_node_decisions(pdir)
        coverage = node_review.coverage_for(stages, decisions)
    else:
        # No workflow → coverage is None (the absence), not a fabricated 0/0 object.
        coverage = None
    workflow = {"present": wf_present, "n_stages": len(stages), "coverage": coverage}

    # ── Versions + runs ──
    n_versions = len(versioning.list_versions(pdir))
    runs = _runs_summary(pdir)

    next_action = _next_action(name, data_model, workflow, runs)

    return {
        "name": name,
        "meta": meta,
        "has_document": has_document,
        # Absolute path string (or None) — a link target, never fabricated.
        "document_path": str(doc_path) if doc_path else None,
        "data_model": data_model,
        "workflow": workflow,
        "versions": n_versions,
        "runs": runs,
        "next_action": next_action,
    }


def _next_action(
    name: str,
    data_model: dict[str, Any],
    workflow: dict[str, Any],
    runs: dict[str, Any],
) -> dict[str, str]:
    """The 'what to do next' rung for this project — first match wins (see the ladder
    in project_state's docstring). Returns {key, label, href}; href is a section path
    under /project/<name> in the unified route map."""
    base = f"/project/{name}"

    # 1. No data model → author it.
    if not data_model["present"]:
        return {
            "key": "author_data_model",
            "label": "Author the data model",
            "href": f"{base}/data_model",
        }
    # 2. Data model present but not approved → approve it.
    if data_model["state"] != "approved":
        return {
            "key": "approve_data_model",
            "label": "Approve the data model",
            "href": f"{base}/data_model",
        }
    # 3. Data model approved, no workflow → build the workflow.
    if not workflow["present"]:
        return {
            "key": "build_workflow",
            "label": "Build the workflow",
            "href": f"{base}/workflow",
        }
    # 4. Workflow present but not fully approved → review the workflow.
    cov = workflow["coverage"] or {}
    if cov.get("approved", 0) < cov.get("total", 0):
        return {
            "key": "review_workflow",
            "label": "Review the workflow",
            "href": f"{base}/workflow",
        }
    # 5. Workflow fully approved but never run → run it (the run button is on /workflow).
    if runs["n"] == 0:
        return {
            "key": "run_workflow",
            "label": "Run the workflow",
            "href": f"{base}/workflow",
        }
    # 6. A run is halted awaiting review → review the run.
    if runs["awaiting_review"] > 0:
        return {
            "key": "review_run",
            "label": "Review the run",
            "href": f"{base}/runs",
        }
    # 7. Nothing outstanding → view runs.
    return {
        "key": "view_runs",
        "label": "View runs",
        "href": f"{base}/runs",
    }


__all__ = [
    "project_meta",
    "write_project_meta",
    "project_state",
]

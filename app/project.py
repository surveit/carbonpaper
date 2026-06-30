"""
project.py — the PROJECT MODEL: one status object per methodology working copy.

A "project" is a methodology directory under examples/<name>/. Over its life it
accumulates, in order: a source DOCUMENT (the pasted methodology), a DATA MODEL
(named schemas/), a WORKFLOW (the compiled/ DAG), immutable VERSIONS, and RUNS.
The unified app shows ONE project at a time with five sections (Overview /
Document / Data model / Workflow / Runs); every section route and the shell that
frames them need the same status snapshot. This module computes that snapshot —
`project_state(mdir)` — so the routes layer never recomputes counts ad hoc and
never disagrees with the sidebar about what's done and what to do next.

Two functions are the public surface:
  - project_meta(mdir)  : the project's identity card (name/title/created/model/
                          source). Reads examples/<name>/project.json when present;
                          for LEGACY projects with none, degrades TRUTHFULLY — never
                          invents a model or a creation date.
  - project_state(mdir) : the status object the Overview + shell render, including
                          the `next_action` ladder ("what to do next").

Dependency rule (mirrors node_review / versioning): this module imports only
stdlib + yaml and the trustworthy interface helpers (node_review, versioning,
web_context). It imports NOTHING from app.main / app.runtime / app.compiler, so it
sits below the routes layer and can be leaned on by both routes and templates.

CARDINAL RULE — never fabricate. Every count here is read off disk (schemas,
compiled stages, versions, runs); when a fact is unknown (a legacy project's
model, a methodology with no runs) the field is None / 0 / a truthful "none"
state, NOT a placeholder. coverage is None (not a zero-coverage object) when there
is no workflow to cover — the absence is reported, not papered over.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from app import node_review
from app import versioning
from app.web_context import _load_schemas


# ─── Document discovery ───────────────────────────────────────────────────────
# A project's source document is the pasted methodology it was authored from. The
# gated-compile flow writes BOTH comp_dir/document.md and methodology_dir/
# methodology_raw.md; legacy/imported projects may carry methodology_raw.md or the
# older methodology_raw.txt. Probe in that order and report the first that exists
# (a truthful path, never a fabricated one).
_DOCUMENT_CANDIDATES = ("document.md", "methodology_raw.md", "methodology_raw.txt")


def _document_path(mdir: Path) -> Path | None:
    """First existing source-document file in <mdir> (see _DOCUMENT_CANDIDATES),
    or None when the project has no document on disk. Returns the absolute Path so
    callers can read or link it; None is a truthful 'no document', not an error."""
    for name in _DOCUMENT_CANDIDATES:
        p = mdir / name
        if p.is_file():
            return p
    return None


# ─── Stage loading (counts / coverage) ────────────────────────────────────────


def _load_compiled_stages(mdir: Path) -> list[dict[str, Any]]:
    """Load the working copy's compiled/ stages, mirroring main.load_stages's loader
    convention (sorted glob, inject _filename/_order, surface a parse error as an
    _error stage rather than dropping it). Returns [] when there is no compiled/ DAG
    yet. Used for counting stages and computing approval coverage — so the count and
    coverage here match exactly what the workflow page loads."""
    compiled_dir = mdir / "compiled"
    if not compiled_dir.is_dir():
        return []
    stages: list[dict[str, Any]] = []
    for yaml_file in sorted(compiled_dir.glob("*.yaml")):
        with yaml_file.open("r", encoding="utf-8") as f:
            try:
                data = yaml.safe_load(f) or {}
            except yaml.YAMLError as exc:
                data = {
                    "id": yaml_file.stem,
                    "name": f"[YAML ERROR] {yaml_file.name}",
                    "type": "python_transform",
                    "compiler_notes": [f"YAML parse error: {exc}"],
                    "_error": True,
                }
        data["_filename"] = yaml_file.name
        data["_order"] = yaml_file.stem.split("_", 1)[0]
        stages.append(data)
    return stages


# ─── Run summary ──────────────────────────────────────────────────────────────


def _runs_summary(mdir: Path) -> dict[str, Any]:
    """Summarise the project's runs/ dir: {n, awaiting_review, latest_status}.

    Mirrors main._list_runs exactly: a run is a child dir of runs/ WITH a readable
    manifest.json; dirs lacking one (partial / legacy-output-only) are not counted,
    so n is the count of real runs, never inflated. `awaiting_review` counts runs
    whose status is 'awaiting_review' (halted at a human_review_queue) — the driver
    of the "review the run" rung of the ladder. `latest_status` is the newest run's
    status (runs are timestamp-id'd, so the max id is newest); None when there are
    no runs. A corrupt manifest is counted (status 'corrupt') rather than hidden."""
    runs_dir = mdir / "runs"
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


def project_meta(mdir: Path) -> dict[str, Any]:
    """The project's identity card: {name, title, created_at, model, source}.

    Reads examples/<name>/project.json when present (the record a gated-authored
    project will carry). For a LEGACY project with no project.json, degrade
    TRUTHFULLY rather than fabricate:
      - name       : the directory name (always known).
      - title      : project.json's title, else None (no invented prose title).
      - created_at : project.json's value, else the earliest RUN id (the oldest
                     thing we can prove happened), else the directory mtime. Never a
                     made-up date.
      - model      : project.json's value, else None — "unknown". We do NOT guess a
                     default model; a wrong provenance is worse than an honest gap.
      - source     : project.json's value, else None.

    Always returns name from the dir even if project.json is malformed, so a corrupt
    file degrades to legacy behaviour instead of raising."""
    mdir = Path(mdir)
    name = mdir.name

    raw: dict[str, Any] = {}
    pj = mdir / "project.json"
    if pj.is_file():
        try:
            loaded = json.loads(pj.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (json.JSONDecodeError, OSError):
            raw = {}

    created_at = raw.get("created_at")
    if not created_at:
        created_at = _earliest_run_id(mdir) or _dir_mtime_iso(mdir)

    return {
        "name": raw.get("name") or name,
        "title": raw.get("title"),
        "created_at": created_at,
        # model is None ("unknown") for legacy — never a fabricated default.
        "model": raw.get("model"),
        "source": raw.get("source"),
    }


def _earliest_run_id(mdir: Path) -> str | None:
    """Oldest run id (the earliest thing we can prove this project did), or None.
    Run ids are strftime timestamps, so the lexical MIN is chronologically first;
    only dirs with a manifest count as real runs (mirrors _runs_summary)."""
    runs_dir = mdir / "runs"
    if not runs_dir.is_dir():
        return None
    ids = [
        r.name for r in runs_dir.iterdir()
        if r.is_dir() and (r / "manifest.json").exists()
    ]
    return min(ids) if ids else None


def _dir_mtime_iso(mdir: Path) -> str | None:
    """Directory mtime as an ISO-8601 string (seconds), or None if it can't be
    stat'd. The last-resort created_at when there is neither a project.json date nor
    a run to date the project from."""
    try:
        return datetime.fromtimestamp(mdir.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return None


def write_project_meta(mdir: Path, **fields: Any) -> dict[str, Any]:
    """Merge `fields` into examples/<name>/project.json and persist it, returning the
    written record. Reads any existing file first so a partial update doesn't drop
    other keys; only the keys passed are overwritten. None values ARE written when
    passed explicitly (so a caller can clear a field), but callers should omit a key
    rather than pass a placeholder — this module never invents a model/date itself."""
    mdir = Path(mdir)
    mdir.mkdir(parents=True, exist_ok=True)
    pj = mdir / "project.json"
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


def project_state(mdir: Path) -> dict[str, Any]:
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
    wins). hrefs are section paths under /methodology/<name> (the unified routes):
      1. no data model            → author it     (/methodology/<id>/data_model)
      2. data model not approved   → approve it     (/methodology/<id>/data_model)
      3. no workflow              → build the workflow (/methodology/<id>/workflow)
      4. workflow approved<total   → review the workflow (/methodology/<id>/workflow)
      5. workflow approved, 0 runs → run it          (/methodology/<id>/workflow)
      6. runs awaiting_review>0    → review the run   (/methodology/<id>/runs)
      7. otherwise                → view runs        (/methodology/<id>/runs)
    """
    mdir = Path(mdir)
    name = mdir.name
    meta = project_meta(mdir)

    # ── Document ──
    doc_path = _document_path(mdir)
    has_document = doc_path is not None

    # ── Data model (named schemas) ──
    schemas = _load_schemas(mdir)
    dm_present = bool(schemas)
    if dm_present:
        dm_state = node_review.data_model_state(mdir, schemas)["state"]
    else:
        # No data model authored yet — report the absence; do NOT run the gate over
        # an empty schema set (that would manufacture an 'unreviewed' verdict for a
        # thing that doesn't exist).
        dm_state = "none"
    data_model = {"present": dm_present, "n_schemas": len(schemas), "state": dm_state}

    # ── Workflow (compiled DAG) ──
    stages = _load_compiled_stages(mdir)
    wf_present = bool(stages)
    if wf_present:
        decisions = node_review.load_node_decisions(mdir)
        coverage = node_review.coverage_for(stages, decisions)
    else:
        # No workflow → coverage is None (the absence), not a fabricated 0/0 object.
        coverage = None
    workflow = {"present": wf_present, "n_stages": len(stages), "coverage": coverage}

    # ── Versions + runs ──
    n_versions = len(versioning.list_versions(mdir))
    runs = _runs_summary(mdir)

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
    under /methodology/<name> in the unified route map."""
    base = f"/methodology/{name}"

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

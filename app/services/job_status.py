"""Background-job status: the ONE shared convention for reporting long-running work
to the UI, used by both the runtime run (`runs/<id>/manifest.json`) and background
generation (`<project>/generation.json`).

Why one module for two files. A run's manifest is stage-granular (one record per
workflow stage, in a per-run directory) while generation is a project-level, singleton
two-phase job (data model → workflow). Their PAYLOADS differ, but they now share:

  * the atomic writer (`atomic_write_json`) — write to a temp sibling then `os.replace`,
    so a concurrent poll never observes a half-written file (issue #95); and
  * the status VOCABULARY — a top-level `status` (`running` → a terminal `ok`/`error`/…),
    `started_at` / `updated_at` / `finished_at` timestamps, a list of per-unit records
    each carrying its own `status` + `error`, and an error shape of
    `{"type", "message", "traceback"}` (matching the runner's per-stage error).

The runner keeps its rich stage manifest; this module owns the generation status file
and the shared atomic write. That is the deliberate "aligned conventions, separate
payloads" decision from issue #95 — not a second bespoke poller.

This module imports only the stdlib, so both `app.runtime` (which must not import the
web app) and `app.web`/`app.services` can depend on it without a cycle.
"""
from __future__ import annotations

import json
import os
import traceback as _tb
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Shared atomic writer ─────────────────────────────────────────────────────


def atomic_write_json(path: Path, payload: Any) -> None:
    """Serialise `payload` as pretty JSON and write it to `path` atomically: write a
    temp sibling in the same directory, then `os.replace` it over the target. A reader
    (a status poll) therefore only ever sees the complete previous file or the complete
    new one — never a half-written mix. Same-directory temp keeps the replace on one
    filesystem, so the rename is atomic. `default=str` mirrors the runner's manifest
    dump (Path/datetime fall back to str) so nothing un-serialisable aborts the write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


# ── Generation status file ───────────────────────────────────────────────────

GENERATION_STATUS_FILE = "generation.json"
# The ordered phases of a generation, in the order the orchestrator runs them.
GENERATION_PHASES = ("data_model", "workflow")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def generation_status_path(project_dir: Path) -> Path:
    return project_dir / GENERATION_STATUS_FILE


def _error_payload(exc: BaseException) -> dict[str, Any]:
    """The shared error shape (matches the runner's per-stage error): type + message +
    a bounded traceback, so a failed generation can be surfaced loudly in the UI."""
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": _tb.format_exc(limit=8),
    }


def init_generation_status(project_dir: Path, *, model: str) -> dict[str, Any]:
    """Write an initial `running` generation status (both phases pending) and return it.
    Called SYNCHRONOUSLY before the background thread starts (mirrors the runner's
    initial `running` manifest in prepare_run), so the page the caller redirects to
    already observes status=running and starts polling immediately."""
    now = _now()
    status: dict[str, Any] = {
        "kind": "generation",
        "status": "running",
        "model": model,
        "started_at": now,
        "updated_at": now,
        "finished_at": None,
        "phases": [
            {"phase": p, "status": "pending", "started_at": None,
             "finished_at": None, "error": None}
            for p in GENERATION_PHASES
        ],
        "error": None,
    }
    _write(project_dir, status)
    return status


def _write(project_dir: Path, status: dict[str, Any]) -> None:
    status["updated_at"] = _now()
    atomic_write_json(generation_status_path(project_dir), status)


def _phase(status: dict[str, Any], phase: str) -> dict[str, Any]:
    for p in status["phases"]:
        if p["phase"] == phase:
            return p
    raise KeyError(f"unknown generation phase {phase!r}")


def phase_running(project_dir: Path, status: dict[str, Any], phase: str) -> None:
    p = _phase(status, phase)
    p["status"] = "running"
    p["started_at"] = _now()
    _write(project_dir, status)


def phase_ok(project_dir: Path, status: dict[str, Any], phase: str) -> None:
    p = _phase(status, phase)
    p["status"] = "ok"
    p["finished_at"] = _now()
    _write(project_dir, status)


def phase_failed(
    project_dir: Path,
    status: dict[str, Any],
    phase: str,
    error: BaseException | str,
) -> None:
    """Record a phase failure loudly: mark the phase (and the whole generation) `error`,
    persisting the error type + message + traceback so the page can render WHY it failed
    instead of the silent "looks like it was never generated" state. `error` accepts an
    exception (full traceback captured) or a plain string (e.g. workflow validation
    issues, which are not raised)."""
    if isinstance(error, BaseException):
        payload = _error_payload(error)
    else:
        payload = {"type": "GenerationError", "message": str(error), "traceback": None}
    p = _phase(status, phase)
    p["status"] = "error"
    p["finished_at"] = _now()
    p["error"] = payload
    status["status"] = "error"
    status["finished_at"] = _now()
    status["error"] = {"phase": phase, **payload}
    _write(project_dir, status)


def generation_done(project_dir: Path, status: dict[str, Any]) -> None:
    """Mark the whole generation successfully complete (all phases ok)."""
    status["status"] = "ok"
    status["finished_at"] = _now()
    _write(project_dir, status)


def load_generation_status(project_dir: Path) -> dict[str, Any] | None:
    """The generation status dict, or None if the project has never generated. A
    corrupt/half-written file reads as None (atomic writes make that near-impossible,
    but a truncated legacy file must not 500 a page load)."""
    path = generation_status_path(project_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def generation_phase(status: dict[str, Any] | None, phase: str) -> dict[str, Any] | None:
    """The record for one phase of a generation status (or None). Lets a route/template
    pick out the phase a given page cares about without list-scanning in Jinja."""
    if not status:
        return None
    for p in status.get("phases", []):
        if p.get("phase") == phase:
            return p
    return None

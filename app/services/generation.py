"""Auto-generation lifecycle: on a fresh document, generate the DATA MODEL and then
the WORKFLOW in the background, writing a status the project page polls.

This is the "kick it off automatically" service behind the data-model page's spinner.
`start_generation` returns immediately, having spawned a daemon thread that runs the
two generation phases in order — data model (an agent call, app.compiler.data_model),
then workflow (app.compiler.compile_methodology) — and records each phase's status in
<project_dir>/generation.json (running → ok / error). A phase that fails is recorded
honestly as `error` with its reason; we never write a fake-success status or fabricate
schemas, and workflow generation does not start if the data model failed.

Reads the poll file back with `read_generation_status`. The CLI-subprocess the agent
spawns runs with the Claude-Code session markers already stripped from os.environ (see
app.compiler.compiler), which this module imports transitively.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.compiler import compile_methodology
from app.compiler.data_model import compile_data_model
from app.services.compilation import regenerate_workflow

_STATUS_FILE = "generation.json"


def start_generation(project_dir: Path, *, document: str, model: str) -> None:
    """Kick off data-model → workflow generation for `project_dir` on a daemon thread
    and return at once. Writes generation.json = {data_model: running, workflow:
    pending} first, so the page shows the spinner immediately; the thread advances the
    statuses as each phase finishes."""
    _write_status(
        project_dir,
        {"data_model": _phase("running"), "workflow": _phase("pending")},
    )
    thread = threading.Thread(
        target=_run_generation,
        args=(project_dir, project_dir.name, document, model),
        daemon=True,
    )
    thread.start()


def read_generation_status(project_dir: Path) -> dict[str, Any] | None:
    """The generation.json poll payload ({data_model, workflow} phase statuses), or
    None if no generation has ever been kicked off for this project."""
    status_path = project_dir / _STATUS_FILE
    if not status_path.exists():
        return None
    return json.loads(status_path.read_text(encoding="utf-8"))


def _run_generation(project_dir: Path, name: str, document: str, model: str) -> None:
    """The daemon-thread body: generate the data model, then (only if it succeeded)
    the workflow. Each phase's failure is recorded as `error` and stops the chain —
    the supervisor boundary that keeps a bad generation from ever reading as success."""
    if not _generate_data_model(project_dir, document, model):
        return
    _generate_workflow(project_dir, name, document, model)


def _generate_data_model(project_dir: Path, document: str, model: str) -> bool:
    """Run the data-model agent call, persist the schemas, mark the phase ok. Returns
    whether it succeeded (False records the phase as `error` and leaves schemas/
    untouched — no partial write)."""
    try:
        schemas = asyncio.run(compile_data_model(document, model=model))
        _persist_schemas(project_dir, schemas)
    except Exception as exc:  # noqa: BLE001 — supervisor boundary: a generation failure is recorded honestly, never faked
        _set_phase(project_dir, "data_model", "error", str(exc))
        return False
    _set_phase(project_dir, "data_model", "ok")
    return True


def _generate_workflow(project_dir: Path, name: str, document: str, model: str) -> None:
    """Compile the workflow from the document and write it. A compile that returns
    validation issues is recorded as `error` (issues joined), not written as a broken
    workflow."""
    _set_phase(project_dir, "workflow", "running")
    try:
        result = compile_methodology(document, name, model=model)
        issues = result["validation"]
        if issues:
            _set_phase(project_dir, "workflow", "error", "; ".join(issues))
            return
        regenerate_workflow(result, project_dir)
    except Exception as exc:  # noqa: BLE001 — supervisor boundary: a generation failure is recorded honestly, never faked
        _set_phase(project_dir, "workflow", "error", str(exc))
        return
    _set_phase(project_dir, "workflow", "ok")


def _persist_schemas(project_dir: Path, schemas: list[dict[str, Any]]) -> None:
    """Replace schemas/ with the generated set — clear stale files a shrinking
    re-generation would leave, then write one NN_<name>.json per schema (the JSON the
    loader globs). Schemas are pre-validated as a SchemaLibrary by compile_data_model,
    so this only writes."""
    schemas_dir = project_dir / "schemas"
    schemas_dir.mkdir(parents=True, exist_ok=True)
    for stale in schemas_dir.glob("*.json"):
        stale.unlink()
    for index, schema in enumerate(schemas, start=1):
        schema_name = schema.get("name") or f"schema{index}"
        path = schemas_dir / f"{index:02d}_{schema_name}.json"
        path.write_text(json.dumps(schema, indent=2, ensure_ascii=False), encoding="utf-8")


# ── generation.json read/write ──────────────────────────────────────────────────

def _phase(status: str, error: str | None = None) -> dict[str, Any]:
    """A fresh phase record. `started_at` is stamped when a phase goes running;
    `finished_at` when it settles."""
    now = _now()
    return {
        "status": status,
        "error": error,
        "started_at": now if status == "running" else None,
        "finished_at": now if status in ("ok", "error") else None,
    }


def _set_phase(
    project_dir: Path, phase: str, status: str, error: str | None = None
) -> None:
    """Advance one phase's status in generation.json, preserving the other phase."""
    status_doc = read_generation_status(project_dir) or {}
    entry = dict(status_doc.get(phase) or {})
    entry["status"] = status
    entry["error"] = error
    now = _now()
    if status == "running":
        entry["started_at"] = now
    if status in ("ok", "error"):
        entry["finished_at"] = now
    status_doc[phase] = entry
    _write_status(project_dir, status_doc)


def _write_status(project_dir: Path, status_doc: dict[str, Any]) -> None:
    """Write generation.json atomically (temp + replace) so a concurrent poll never
    reads a half-written file."""
    path = project_dir / _STATUS_FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(status_doc, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")

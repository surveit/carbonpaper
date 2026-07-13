"""Auto-generation: on a fresh document, generate the DATA MODEL and then the WORKFLOW
in the background, reporting live status to the UI.

`start_generation` writes an initial `running` status file (`<project>/generation.json`)
SYNCHRONOUSLY and then spawns a daemon thread that runs the two phases in order — data
model (an agent call, app.compiler.data_model) then workflow (app.compiler.compile_
methodology) — writing each result to the project directory and updating the status file
atomically as each phase starts / succeeds / fails. The data-model and workflow pages
poll that file (`GET /project/<name>/generation-status`) to show a spinner while it runs
and to surface a FAILURE loudly instead of the old silent "no schemas on disk" state.

The status file follows the shared background-job convention in
`app.services.job_status` (the same atomic-write + status vocabulary the runtime
`manifest.json` uses); see that module for the deliberate "aligned conventions, separate
payloads" decision (issue #95). A phase that fails is LOGGED (never fabricated as
success) and stops the chain — the workflow is not built on a data model that failed —
and the failure is recorded on the status file so the page can render it.

The CLI subprocess the agent spawns runs with the Claude-Code session markers already
stripped from os.environ (see app.compiler.compiler), which this module imports
transitively.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any

from app.compiler import compile_methodology
from app.compiler.data_model import compile_data_model
from app.models.named_schemas import SchemaLibrary
from app.services import job_status
from app.services.compilation import regenerate_workflow

_log = logging.getLogger(__name__)


def start_generation(project_dir: Path, *, document: str, model: str) -> None:
    """Kick off data-model → workflow generation for `project_dir` on a daemon thread
    and return at once. Writes the initial `running` status file synchronously first so
    the page the caller redirects to already sees status=running and starts polling.
    Results land in schemas/ and compiled/; the pages read them from disk once each
    phase completes."""
    status = job_status.init_generation_status(project_dir, model=model)
    thread = threading.Thread(
        target=_run_generation,
        args=(project_dir, project_dir.name, document, model, status),
        daemon=True,
    )
    thread.start()


def _run_generation(
    project_dir: Path, name: str, document: str, model: str, status: dict[str, Any]
) -> None:
    """The daemon-thread body: generate the data model, then (only if it succeeded) the
    workflow. Each phase logs its own failure, records it on the status file, and stops
    the chain — the supervisor boundary that keeps a bad generation from being built on.
    If both phases succeed the generation is marked done (status=ok)."""
    if not _generate_data_model(project_dir, document, model, status):
        return
    if not _generate_workflow(project_dir, name, document, model, status):
        return
    job_status.generation_done(project_dir, status)


def _generate_data_model(
    project_dir: Path, document: str, model: str, status: dict[str, Any]
) -> bool:
    """Run the data-model agent call and persist the schemas. Returns whether it
    succeeded; a failure is logged, recorded on the status file (so the page shows it
    loudly), and leaves schemas/ untouched (no partial write)."""
    job_status.phase_running(project_dir, status, "data_model")
    try:
        library = asyncio.run(compile_data_model(document, model=model))
        _persist_schemas(project_dir, library)
    except Exception as exc:  # noqa: BLE001 — supervisor boundary: log the failure, never fake a success
        _log.exception("data-model generation failed for project %r", project_dir.name)
        job_status.phase_failed(project_dir, status, "data_model", exc)
        return False
    job_status.phase_ok(project_dir, status, "data_model")
    return True


def _generate_workflow(
    project_dir: Path, name: str, document: str, model: str, status: dict[str, Any]
) -> bool:
    """Compile the workflow from the document and write it. A compile that returns
    validation issues, or raises, is logged, recorded on the status file, and not
    written as a broken workflow. Returns whether it succeeded."""
    job_status.phase_running(project_dir, status, "workflow")
    try:
        result = compile_methodology(document, name, model=model)
        issues = result["validation"]
        if issues:
            _log.error("workflow generation for %r produced issues: %s", name, issues)
            job_status.phase_failed(
                project_dir, status, "workflow",
                f"compiled workflow failed validation: {'; '.join(issues)}",
            )
            return False
        regenerate_workflow(result, project_dir)
    except Exception as exc:  # noqa: BLE001 — supervisor boundary: log the failure, never fake a success
        _log.exception("workflow generation failed for project %r", name)
        job_status.phase_failed(project_dir, status, "workflow", exc)
        return False
    job_status.phase_ok(project_dir, status, "workflow")
    return True


def _persist_schemas(project_dir: Path, library: SchemaLibrary) -> None:
    """Replace schemas/ with the generated data model — clear stale files a shrinking
    re-generation would leave, then write one NN_<name>.json per schema. The library is
    already validated by compile_data_model, so this only writes."""
    schemas_dir = project_dir / "schemas"
    schemas_dir.mkdir(parents=True, exist_ok=True)
    for stale in schemas_dir.glob("*.json"):
        stale.unlink()
    for index, schema in enumerate(library.schemas, start=1):
        payload = schema.model_dump(mode="json", exclude_none=True)
        path = schemas_dir / f"{index:02d}_{schema.name}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

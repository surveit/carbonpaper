"""Auto-generation: on a fresh document, generate the DATA MODEL and then the WORKFLOW
in the background, writing the results to disk.

`start_generation` returns immediately, having spawned a daemon thread that runs the two
phases in order — data model (an agent call, app.compiler.data_model) then workflow
(app.compiler.compile_methodology) — and writes each to the project directory. The
pages then simply read whatever is on disk (schemas/, compiled/); there is no status
file or spinner yet (real-time status is tracked in issue #95). A phase that fails is
LOGGED (never fabricated as success) and stops the chain — the workflow is not built on
a data model that failed.

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

from app.agent.store import open_session_store, save_transcript_session
from app.compiler import compile_methodology
from app.compiler.data_model import build_data_model_agent
from app.models.named_schemas import SchemaLibrary
from app.services.compilation import regenerate_workflow

_log = logging.getLogger(__name__)


def start_generation(project_dir: Path, *, document: str, model: str) -> None:
    """Kick off data-model → workflow generation for `project_dir` on a daemon thread
    and return at once. Results land in schemas/ and compiled/; the pages read them
    from disk on the next load."""
    thread = threading.Thread(
        target=_run_generation,
        args=(project_dir, project_dir.name, document, model),
        daemon=True,
    )
    thread.start()


def _run_generation(project_dir: Path, name: str, document: str, model: str) -> None:
    """The daemon-thread body: generate the data model, then (only if it succeeded) the
    workflow. Each phase logs its own failure and stops the chain — the supervisor
    boundary that keeps a bad generation from being built on."""
    if not _generate_data_model(project_dir, document, model):
        return
    _generate_workflow(project_dir, name, document, model)


def _generate_data_model(project_dir: Path, document: str, model: str) -> bool:
    """Run the data-model agent, persist the schemas, and persist the agent conversation
    as a viewable chat session. Returns whether it succeeded; a failure is logged, leaves
    schemas/ untouched (no partial write), and STILL persists the conversation with the
    error surfaced — so a failed generation is visible rather than silent."""
    agent = build_data_model_agent(document, model=model)
    try:
        library = asyncio.run(agent.run())
    except Exception as exc:  # noqa: BLE001 — supervisor boundary: log the failure, never fake a success
        _persist_conversation(project_dir, agent.transcript, error=exc)
        _log.exception("data-model generation failed for project %r", project_dir.name)
        return False
    _persist_schemas(project_dir, library)
    _persist_conversation(project_dir, agent.transcript)
    return True


def _generate_workflow(project_dir: Path, name: str, document: str, model: str) -> None:
    """Compile the workflow from the document and write it. A compile that returns
    validation issues, or raises, is logged and not written as a broken workflow."""
    try:
        result = compile_methodology(document, name, model=model)
        issues = result["validation"]
        if issues:
            _log.error("workflow generation for %r produced issues: %s", name, issues)
            return
        regenerate_workflow(result, project_dir)
    except Exception:  # noqa: BLE001 — supervisor boundary: log the failure, never fake a success
        _log.exception("workflow generation failed for project %r", name)


def _persist_schemas(project_dir: Path, library: SchemaLibrary) -> None:
    """Replace schemas/ with the generated data model — clear stale files a shrinking
    re-generation would leave, then write one NN_<name>.json per schema. The library is
    already validated by the data-model agent, so this only writes."""
    schemas_dir = project_dir / "schemas"
    schemas_dir.mkdir(parents=True, exist_ok=True)
    for stale in schemas_dir.glob("*.json"):
        stale.unlink()
    for index, schema in enumerate(library.schemas, start=1):
        payload = schema.model_dump(mode="json", exclude_none=True)
        path = schemas_dir / f"{index:02d}_{schema.name}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _persist_conversation(
    project_dir: Path, transcript: list[dict[str, Any]], *, error: Exception | None = None
) -> None:
    """Persist the data-model agent conversation as a view-only chat session so it is
    openable in the chat UI. On failure, append a clear failure note so the session
    surfaces the error rather than merely looking unfinished. Persisting is best-effort:
    a store error is logged, never allowed to mask the generation outcome."""
    messages = list(transcript)
    if error is not None:
        messages.append({
            "role": "assistant",
            "parts": [{"type": "text", "text": f"⚠ data-model generation failed: {error}"}],
        })
    try:
        save_transcript_session(
            open_session_store(),
            transcript=messages,
            title=f"Generation · data model · {project_dir.name}",
            context={"project_id": project_dir.name, "phase": "data_model"},
        )
    except Exception:  # noqa: BLE001 — persistence is a secondary effect: log, never mask the run outcome
        _log.exception("failed to persist data-model conversation for project %r", project_dir.name)

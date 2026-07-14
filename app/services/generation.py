"""Auto-generation: on a fresh document, generate the DATA MODEL then the WORKFLOW,
writing the results to disk.

The data-model phase runs as a LIVE chat turn: `start_generation` delegates to
`app.compiler.data_model.start_data_model_turn` (the bridge onto the app.agent spine),
which creates a chat session and streams the data-model agent to /chat/<sid> as it
authors the schemas. When the turn finishes with a valid submission, the schemas are
written and the WORKFLOW phase is kicked on a background thread — it is a blocking
raw-completion (compile_methodology), not an agent conversation, so it stays off the
event loop. A phase that fails is surfaced in the live turn / logged (never fabricated as
success) and never built on.

The CLI subprocess the agent spawns runs with the Claude-Code session markers already
stripped from os.environ (see app.compiler.compiler), which this module imports
transitively.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from app.compiler import compile_methodology
from app.compiler.data_model import start_data_model_turn
from app.models.named_schemas import SchemaLibrary
from app.services.compilation import regenerate_workflow

_log = logging.getLogger(__name__)


def start_generation(project_dir: Path, *, document: str, model: str) -> str:
    """Kick off data-model → workflow generation and return the id of the chat session
    streaming the data-model conversation. The data-model agent runs as a LIVE turn
    (watchable at /chat/<sid>, persisted when it ends); on a valid submission its schemas
    are written and the workflow phase is kicked. Must be called from the server event
    loop — the underlying turn is started there."""
    return start_data_model_turn(
        document=document,
        project_name=project_dir.name,
        model=model,
        on_answer=lambda answer: _finish_data_model(project_dir, document, model, answer),
    )


def _finish_data_model(
    project_dir: Path, document: str, model: str, answer: SchemaLibrary | None
) -> None:
    """Completion hook for the data-model turn: if the agent submitted a valid data model
    (`answer`), persist the schemas and kick the workflow phase; otherwise the failure was
    already streamed to the live turn and there is nothing valid to build the workflow on."""
    if answer is None:
        return
    _persist_schemas(project_dir, answer)
    _start_workflow(project_dir, document, model)


def _start_workflow(project_dir: Path, document: str, model: str) -> None:
    """Run the workflow phase on a daemon thread — it is a blocking raw-completion
    (compile_methodology), so it must not run on the event loop the live turn uses."""
    threading.Thread(
        target=_generate_workflow,
        args=(project_dir, project_dir.name, document, model),
        daemon=True,
    ).start()


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

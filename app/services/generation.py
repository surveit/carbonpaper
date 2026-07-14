"""Auto-generation: on a fresh document, generate the DATA MODEL as a live chat turn and write
the schemas. The WORKFLOW is NOT auto-built — the create-flow stops at the data model so it can
be reviewed and approved first; the workflow is then generated on demand (grounded on that
approved model) via start_workflow_generation.

Both turns run through the app.compiler bridges (start_data_model_generation_agent /
start_workflow_generation_agent):
app.compiler owns the app.agent spine, so this orchestration delegates there rather than
importing the spine directly. `start_generation` streams the data-model agent to /chat/<sid>;
on a valid submission its schemas are written. `start_workflow_generation` is the manual
workflow build — clicking "Generate workflow" runs the workflow agent as a live turn and
returns its session id (the route lands the user on /chat/<sid>); it compiles ONLY the
workflow, grounding it in the approved data model, without touching schemas/. A phase that
fails is surfaced in the live turn / logged, never fabricated as success.

The turns run on the server event loop, so every `start_*` entry here must be called from an
async context. The CLI subprocess the agents spawn runs with the Claude-Code session markers
already stripped from os.environ (see app.compiler.compiler), imported transitively via the
bridges.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.compiler.data_model import start_data_model_generation_agent
from app.compiler.workflow import start_workflow_generation_agent
from app.models.named_schemas import SchemaLibrary
from app.models.workflow import Workflow
from app.services.compilation import regenerate_workflow
from app.services.loader import stage_to_spec_dict

_log = logging.getLogger(__name__)


def start_generation(project_dir: Path, *, document: str, model: str) -> str:
    """Kick off DATA-MODEL generation and return the id of the chat session streaming the
    conversation. The data-model agent runs as a LIVE turn (watchable at /chat/<sid>, persisted
    when it ends); on a valid submission its schemas are written. The workflow is NOT auto-built
    — the create-flow stops at the data model so it can be reviewed/approved first. Must be
    called from the server event loop — the underlying turn is started there."""
    return start_data_model_generation_agent(
        document=document,
        project_name=project_dir.name,
        model=model,
        on_answer=lambda answer: _finish_data_model(project_dir, answer),
    )


def start_workflow_generation(
    project_dir: Path,
    *,
    document: str,
    model: str,
    data_model: SchemaLibrary | None,
) -> str:
    """Run the WORKFLOW agent as a LIVE chat turn and return its session id (the caller lands
    the user on /chat/<sid>). Compiles ONLY the workflow — schemas/ is untouched — grounding it
    in `data_model` (the approved schemas) when given. Must be called from the server event
    loop."""
    name = project_dir.name
    return start_workflow_generation_agent(
        document=document,
        project_name=name,
        model=model,
        data_model=data_model,
        on_answer=lambda answer: _finish_workflow(project_dir, name, answer),
    )


def _finish_data_model(project_dir: Path, answer: SchemaLibrary | None) -> None:
    """Completion hook for the data-model turn (runs on the event loop): if the agent submitted
    a valid data model (`answer`), persist the schemas. The create-flow stops here — the
    workflow is built later, on demand, from the reviewed data model. A failed submission was
    already streamed to the live turn; there is nothing to persist."""
    if answer is None:
        return
    _persist_schemas(project_dir, answer)


def _finish_workflow(project_dir: Path, name: str, answer: Workflow | None) -> None:
    """Completion hook for the workflow turn: if the agent submitted a valid Workflow, write it
    (schemas/ untouched); otherwise the failure was already streamed to the live turn."""
    if answer is None:
        return
    regenerate_workflow(_workflow_result(answer, name), project_dir)


def _workflow_result(workflow: Workflow, name: str) -> dict[str, Any]:
    """Shape a validated Workflow into the dict write_methodology persists: the stages in
    canonical on-disk form, with a clean validation list (the agent only submits a workflow that
    already validates). The agent carries the shape through the tool, so there is no prose
    methodology_raw write-up and no top-level compiler_notes — any per-stage notes ride along on
    each stage."""
    return {
        "name": name,
        "stages": [stage_to_spec_dict(s) for s in workflow.stages],
        "methodology_raw": "",
        "compiler_notes": None,
        "validation": [],
    }


def _persist_schemas(project_dir: Path, library: SchemaLibrary) -> None:
    """Replace schemas/ with the generated data model — clear stale files a shrinking
    re-generation would leave, then write one NN_<name>.json per schema. The library is already
    validated by the data-model agent, so this only writes."""
    schemas_dir = project_dir / "schemas"
    schemas_dir.mkdir(parents=True, exist_ok=True)
    for stale in schemas_dir.glob("*.json"):
        stale.unlink()
    for index, schema in enumerate(library.schemas, start=1):
        payload = schema.model_dump(mode="json", exclude_none=True)
        path = schemas_dir / f"{index:02d}_{schema.name}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

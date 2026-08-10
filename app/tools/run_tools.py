"""The run tools BOTH authoring surfaces bind, over the one app.services.run seam:
start a production run, then block once until it settles. Held here so the editing
agent and the tour cannot drift on what starting or awaiting a run returns."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel

from app.services import run as run_service

# Long enough for a workflow whose model stage spawns a few calls, short enough to sit
# well inside any transport's idle limit. A caller that comes back not-terminal waits again.
DEFAULT_WAIT_SECONDS = 120


class RunStarted(BaseModel):
    run_id: str
    version_id: str
    status: str
    run_url: str


def start_run_of_stored_workflow(
    project_id: str, version_id: str, limits: dict[str, int] | None, *, base_url: str
) -> RunStarted:
    """`base_url` ends in "/"; "/" alone yields the in-app path."""
    run_id = run_service.start_run(
        project_id, version_id=version_id or None, limits=limits
    )
    status = run_service.read_run_status(project_id, run_id)
    return RunStarted(
        run_id=run_id,
        version_id=run_service.read_pinned_version(project_id, run_id),
        status=str(status["status"]),
        run_url=f"{base_url}project/{project_id}/runs/{run_id}",
    )


def wait_for_started_run(
    project_id: str, run_id: str, timeout_seconds: int
) -> run_service.RunOutcome:
    return run_service.wait_for_run_to_finish(
        project_id, run_id, timeout_seconds=timeout_seconds or DEFAULT_WAIT_SECONDS
    )


RUN_TOOL_SCHEMAS: dict[str, dict[str, object]] = {
    "run_workflow": {
        "project_id": Annotated[str, "The project whose stored workflow to run."],
        "version_id": Annotated[
            str,
            'Optional: the stored version to run. Pass "" for the newest stored one, '
            "or the version_id an earlier run reported to re-run that same workflow.",
        ],
        "limits": Annotated[
            dict[str, int],
            'A per-stage row cap, {"<stage id>": N} — that stage READS only the first '
            "N rows. Omit it to run the whole bound source.",
        ],
    },
    "wait_for_run": {
        "project_id": Annotated[str, "The project the run belongs to."],
        "run_id": Annotated[str, "The run id run_workflow returned."],
        "timeout_seconds": Annotated[
            int,
            f"How long to block before reporting back, default {DEFAULT_WAIT_SECONDS}. "
            "Passing 0 uses that default.",
        ],
    },
}

RUN_TOOL_LABELS: dict[str, str] = {
    "run_workflow": "Starting a run",
    "wait_for_run": "Waiting for the run to finish",
}

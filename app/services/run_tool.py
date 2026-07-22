"""The authoring-loop run seam: a `RunTool` protocol, a raising stub, and a
manifest reader.

`RunTool` is the narrow interface the authoring loop drives runs through —
starting a run and reading its outcome — so services never import the runner
(the app.services ↛ app.runtime contract). `StubRunTool` is the seam's current
implementation: reading finished runs off disk is real, but STARTING a run is
not wired to any runner yet, so `start_run` fails loudly.

`limits`/`offsets` carry exactly `app.runtime.runner.prepare_run`'s per-connector
row-slicing semantics ({stage_id: N}); the seam adds no vocabulary of its own.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from app.core.errors import RunNotFoundError, RunToolUnavailableError
from app.models.run_manifest import RunManifest, StageRunRecord

__all__ = [
    "RunManifest",
    "RunNotFoundError",
    "RunTool",
    "StageRunRecord",
    "StubRunTool",
    "read_run_manifest",
]


class RunTool(Protocol):
    """Start a run and read its outcome, without exposing the runner. The
    authoring loop holds a `RunTool`, not the runner, so it can be driven by a
    fake in tests and stays on the services side of the runtime boundary."""

    def start_run(
        self,
        project_dir: Path,
        *,
        version_id: str,
        limits: dict[str, int] | None = None,
        offsets: dict[str, int] | None = None,
    ) -> str: ...

    def run_status(self, project_dir: Path, run_id: str) -> RunManifest: ...


class StubRunTool:
    """The seam's placeholder RunTool. `run_status` reads a real on-disk run;
    `start_run` raises until an external run tool is wired in."""

    def start_run(
        self,
        project_dir: Path,
        *,
        version_id: str,
        limits: dict[str, int] | None = None,
        offsets: dict[str, int] | None = None,
    ) -> str:
        raise RunToolUnavailableError(
            "starting runs through the authoring loop is not wired yet — the "
            "external run tool that drives app.runtime.runner is still pending; "
            "only reading finished runs (run_status) is available."
        )

    def run_status(self, project_dir: Path, run_id: str) -> RunManifest:
        return read_run_manifest(project_dir, run_id)


def read_run_manifest(project_dir: Path, run_id: str) -> RunManifest:
    """Parse `<project_dir>/runs/<run_id>/manifest.json` into a `RunManifest`.

    A missing run directory or manifest raises `RunNotFoundError` naming the
    path rather than fabricating an empty manifest."""
    manifest_path = Path(project_dir) / "runs" / run_id / "manifest.json"
    if not manifest_path.exists():
        raise RunNotFoundError(f"no run manifest at {manifest_path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    return RunManifest.model_validate(payload)

"""The files a run's publish stages wrote, addressed at whichever surface lists them."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel

from app.core.run_status import RunStatus, StageStatus
from app.web.file_sizes import describe_bytes
from app.web.stage_strip import read_stage_records


class PublishedFile(BaseModel):
    """`note` is what a stranger reads before opening it: size, and where it sits."""

    name: str
    href: str
    note: str | None = None


def list_published_files(
    project_id: str, run_id: str, run_dir: Path, manifest: Mapping[str, Any]
) -> list[PublishedFile]:
    if manifest.get("status") in (RunStatus.RUNNING, None):
        return []
    has_ok_publish = any(
        record.get("type") == "publish"
        and record.get("status") in (StageStatus.OK, StageStatus.VALIDATION_WARNINGS)
        for record in read_stage_records(manifest)
    )
    artifacts_root = run_dir / "artifacts"
    if not (has_ok_publish and artifacts_root.is_dir()):
        return []
    files = sorted(f for f in artifacts_root.rglob("*") if f.is_file())
    index = next((f for f in files if f.name == "index.html"), None)
    if index is not None:
        files = [index]
    return [
        PublishedFile(
            name=f.name,
            href=(f"/project/{project_id}/runs/{run_id}/artifact/"
                  f"{f.relative_to(artifacts_root).as_posix()}"),
            note=describe_bytes(f.stat().st_size),
        )
        for f in files
    ]

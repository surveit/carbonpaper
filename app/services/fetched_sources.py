"""Turning a fetch connector's URL into a local file the run reads.

Downloaded at prepare, into the same content-addressed store an upload lands in, and
handed to the runner as an ordinary path binding — so the runtime reads one kind of
source and a run manifest records a fetched source exactly as it records a bound file.
"""
from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO, ClassVar
from urllib.parse import urlparse

from app.core.persistence import PersistedModel, PersistenceScope
from app.models.schema import StageId, TypeUnsafeUserStageConfigOverride
from app.models.stages.input_data import ConnectorKind, InputDataStage
from app.models.workflow import Workflow
from app.services.errors import FileNotStoredError, SourceFetchError
from app.services.uploads import open_project_file, save_upload

# Says who is calling and where to complain, the courtesy a public data host is owed.
USER_AGENT = "carbon-paper (+https://github.com/shuhanbao/carbonpaper)"

_READ_TIMEOUT_SECONDS = 300
_FALLBACK_FILENAME = "fetched.dat"


class FetchedSource(PersistedModel):
    """One project's memo of what a URL returned; the bytes sit in the upload store."""

    collection: ClassVar[str] = "fetched_source"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    url: str
    sha256: str
    project_id: str


def bind_fetched_sources(
    workflow: Workflow,
    project_id: str,
    bindings: Mapping[StageId, TypeUnsafeUserStageConfigOverride] | None,
) -> dict[StageId, TypeUnsafeUserStageConfigOverride]:
    """Add a `path` binding for every fetch stage the caller has not already bound."""
    resolved: dict[StageId, TypeUnsafeUserStageConfigOverride] = {
        stage_id: dict(params) for stage_id, params in (bindings or {}).items()
    }
    for stage in _fetch_stages(workflow):
        if "path" in resolved.get(stage.id, {}):
            continue
        url = str(stage.connector.params["url"])   # required on the model
        resolved.setdefault(stage.id, {})["path"] = str(resolve_fetched_path(url, project_id))
    return resolved


def resolve_fetched_path(url: str, project_id: str, *, refetch: bool = False) -> Path:
    """Holding the first copy is what lets a re-run mean what the first run meant."""
    if not refetch:
        held = _find_held_copy(url, project_id)
        if held is not None:
            return held
    return _fetch_and_record(url, project_id)


def _fetch_stages(workflow: Workflow) -> list[InputDataStage]:
    return [
        stage for stage in workflow.stages
        if isinstance(stage, InputDataStage) and stage.connector.kind == ConnectorKind.fetch
    ]


def _find_held_copy(url: str, project_id: str) -> Path | None:
    """None covers both no memo and a memo whose bytes someone has since deleted."""
    memo = _find_memo(url, project_id)
    if memo is None:
        return None
    try:
        _, path = open_project_file(project_id, memo.sha256)
    except FileNotStoredError:
        return None
    return path


def _fetch_and_record(url: str, project_id: str) -> Path:
    with _open_stream(url) as response:
        stored = save_upload(_filename_for(url), response, project_id)
    memo = _find_memo(url, project_id) or FetchedSource(
        url=url, sha256=stored.sha256, project_id=project_id
    )
    memo.sha256 = stored.sha256
    memo.save()
    _, path = open_project_file(project_id, stored.sha256)
    return path


def _find_memo(url: str, project_id: str) -> FetchedSource | None:
    held = [r for r in FetchedSource.list() if r.url == url and r.project_id == project_id]
    return held[0] if held else None


def _open_stream(url: str) -> BinaryIO:
    """urlopen's own errors name neither the URL nor what wanted it."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        return urllib.request.urlopen(request, timeout=_READ_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as refused:
        raise SourceFetchError(
            f"{url} answered {refused.code} {refused.reason}. A dataset behind a bot wall "
            "has to be fetched by hand and uploaded as a file input instead."
        ) from refused
    except urllib.error.URLError as unreachable:
        raise SourceFetchError(
            f"{url} could not be reached: {unreachable.reason}"
        ) from unreachable


def _filename_for(url: str) -> str:
    """The published name, so the files list shows the reader something recognisable."""
    name = Path(urlparse(url).path).name
    return name if name and name not in (".", "..") else _FALLBACK_FILENAME

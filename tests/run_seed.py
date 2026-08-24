"""Read and seed a run's stored manifest directly, for tests.

Production writes go through `app.runtime.manifest.write_manifest`; these do not,
so a test can store the exact payload it means to — including one today's model
would refuse, which is the case the tolerant run listing exists for.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.persistence import get_store
from app.models.records.run_manifest import PRODUCTION_RUNS, RunManifest

COLLECTION = "run"


def read_manifest(project: str | Path, run_id: str) -> dict[str, Any]:
    """The stored payload, unvalidated. KeyError-ish `DocumentNotFound` if absent."""
    return get_store().read(COLLECTION, _key(project, run_id))


def store_manifest(project: str | Path, run_id: str, payload: dict[str, Any]) -> None:
    get_store().write(COLLECTION, _key(project, run_id), payload)


def store_manifest_text(project: str | Path, run_id: str, text: str) -> None:
    """Store raw TEXT, so a test can plant a payload that is not even JSON."""
    key = _key(project, run_id)
    get_store().write(COLLECTION, key, {})
    get_store()._conn.execute(  # type: ignore[attr-defined]
        "UPDATE documents SET data=? WHERE collection=? AND id=?", (text, COLLECTION, key))
    get_store()._conn.commit()  # type: ignore[attr-defined]


def manifest_exists(project: str | Path, run_id: str) -> bool:
    return get_store().exists(COLLECTION, _key(project, run_id))


def list_run_ids(project: str | Path) -> list[str]:
    prefix = f"{_name(project)}/"
    return [doc_id[len(prefix):] for doc_id in get_store().list_ids(COLLECTION, prefix)]


def manifest_text(project: str | Path, run_id: str) -> str:
    """The stored payload as JSON text, for a test asserting on its content."""
    return json.dumps(read_manifest(project, run_id))


def _key(project: str | Path, run_id: str, area: str = PRODUCTION_RUNS) -> str:
    return RunManifest.compose_id(_name(project), run_id, area)


def _name(project: str | Path) -> str:
    return Path(project).name


def store_events(project: str | Path, run_id: str, events: list[dict[str, Any]]) -> None:
    """Seed a run's event log straight into its chunks, past the writer thread."""
    from app.models.records.run_events import RunEventChunk
    from app.runtime.run_log import CHUNK_SIZE

    grouped: dict[int, list[dict[str, Any]]] = {}
    for event in events:
        grouped.setdefault(int(event["seq"]) // CHUNK_SIZE, []).append(event)
    name = _name(project)
    for index, chunk_events in grouped.items():
        RunEventChunk(
            id=RunEventChunk.compose_id(name, run_id, index), events=chunk_events
        ).save()

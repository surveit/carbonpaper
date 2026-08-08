"""a project's identity record is backfilled from its project.json

Revision ID: 0010
Revises: 0009
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from alembic import op
from sqlalchemy.engine import Connection

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

# The last reader of examples/<name>/project.json. A project's identity is now the
# `project` record in the document store alone; nothing under app/ reads or writes
# that file (tests/arch/test_project_json_is_dead.py holds that), so a project.json
# left on disk after this revision is inert.
_COLLECTION = "project"
_FILENAME = "project.json"

# Frozen copies of what this revision was written against, not imports that would
# move underneath it: project.json's key -> the Project field it lands in, plus the
# key holding what is now the record's id.
_FIELD_BY_KEY = {
    "title": "title",
    "model": "model",
    "source": "source",
    "created_at": "authored_at",
}
_ID_KEY = "name"
# app.services.workspace.projects_dir + configure_projects_dir_from_env, as of 0010.
_PROJECTS_DIR_ENV = "CARBONPAPER_PROJECTS_DIR"
_DEFAULT_PROJECTS_ROOT = Path(__file__).resolve().parents[2] / "examples"


class UnbackfillableProject(Exception):
    """A project whose identity the files on disk do not determine."""


def upgrade() -> None:
    refused = backfill_project_records(op.get_bind(), resolve_projects_root())
    if refused:
        # Raising rolls the whole revision back, so a refusal leaves EVERY project
        # untouched, not just the ones named. Re-running after the fix is safe: a
        # project whose record already agrees with its file is skipped, not rewritten.
        raise UnbackfillableProject(
            "the files on disk do not determine these projects' identity, and this "
            "revision will not guess one: " + "; ".join(refused) + ". Correct each "
            "project.json (or delete it, if the stored record is the right one) and "
            "re-run. Nothing was written."
        )


def downgrade() -> None:
    raise NotImplementedError(
        "0010 is not reversible: a project record written here is indistinguishable "
        "from one the app wrote itself, so removing them would delete live identities"
    )


def backfill_project_records(connection: Connection, root: Path) -> list[str]:
    """Returns one line per REFUSED project; those are written nowhere."""
    refused: list[str] = []
    created: list[str] = []
    agreed: list[str] = []
    for name, path in _find_project_files(root):
        try:
            record = _plan_record(name, _read_document(path), _stored_record(connection, name))
        except UnbackfillableProject as exc:
            refused.append(f"{name}: {exc}")
            continue
        if record is None:
            agreed.append(name)
            continue
        connection.exec_driver_sql(
            "INSERT INTO documents (collection, id, data, schema_version) VALUES (?, ?, ?, 1)",
            (_COLLECTION, record["id"], json.dumps(record)),
        )
        created.append(name)
    # Nothing runs this revision automatically, so the operator supplies the projects
    # root by environment and gets no other feedback that it was the right one. Naming
    # it beside the counts is what separates "a fresh install with nothing to carry"
    # from "pointed at the wrong directory" — the two look identical otherwise.
    #
    # A wrong root is a SUCCESS to alembic: it stamps 0010 and `alembic upgrade head`
    # will not run this again, so the printed counts are the only warning the operator
    # gets. Recovery, once CARBONPAPER_PROJECTS_DIR points at the real root — move the
    # marker back by hand, then upgrade again:
    #
    #     alembic stamp 0009
    #     alembic upgrade head
    #
    # Safe to repeat: a project whose stored record already agrees with its file is
    # skipped, not rewritten. `alembic downgrade` is NOT the route — downgrade() refuses
    # on purpose, and stamping does not run it.
    print(summarize_backfill(root, created, agreed, refused))
    return refused


def summarize_backfill(
    root: Path, created: list[str], agreed: list[str], refused: list[str]
) -> str:
    missing = "" if root.is_dir() else " — NO SUCH DIRECTORY"
    return (
        f"0010: projects root {root}{missing}\n"
        f"0010: {len(created)} record(s) created {sorted(created)}, "
        f"{len(agreed)} already agreed, {len(refused)} refused"
    )


def _find_project_files(root: Path) -> list[tuple[str, Path]]:
    if not root.is_dir():
        return []
    # A project with no project.json at all is a legacy project: nothing on disk
    # states its identity, so there is nothing to carry into a record.
    return [
        (project_dir.name, project_dir / _FILENAME)
        for project_dir in sorted(path for path in root.iterdir() if path.is_dir())
        if (project_dir / _FILENAME).is_file()
    ]


def resolve_projects_root() -> Path:
    configured = os.environ.get(_PROJECTS_DIR_ENV)
    return Path(configured) if configured else _DEFAULT_PROJECTS_ROOT


def _plan_record(
    name: str, document: dict[str, Any], stored: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The record to write for `name`, or None when a stored one already agrees with
    `document`. Raises UnbackfillableProject when the two disagree."""
    _validate_document(name, document)
    fields = {field: document[key] for key, field in _FIELD_BY_KEY.items() if key in document}
    if stored is None:
        return _new_record(name, fields)
    disagreements = [
        f"{field}: {_FILENAME} says {value!r}, the stored record says {stored.get(field)!r}"
        for field, value in fields.items()
        if stored.get(field) != value
    ]
    if disagreements:
        raise UnbackfillableProject("; ".join(disagreements))
    return None


def _new_record(name: str, fields: dict[str, Any]) -> dict[str, Any]:
    # created_at/updated_at stamp when this RECORD was written — now — which is what
    # PersistedModel means by them. The project's own creation date is authored_at,
    # and it stays None unless project.json actually carries one.
    stamp = datetime.now().isoformat(timespec="microseconds")
    return {
        "id": name,
        "created_at": stamp,
        "updated_at": stamp,
        "title": None,
        "model": None,
        "source": None,
        "authored_at": None,
        **fields,
    }


def _validate_document(name: str, document: dict[str, Any]) -> None:
    if document.get(_ID_KEY, name) != name:
        raise UnbackfillableProject(
            f"{_FILENAME} names the project {document[_ID_KEY]!r}, but it sits in {name}/"
        )
    unknown = sorted(set(document) - set(_FIELD_BY_KEY) - {_ID_KEY})
    if unknown:
        # Refused rather than dropped: a key this revision does not know is a fact
        # about the project that would vanish on the way into the record.
        raise UnbackfillableProject(f"{_FILENAME} carries unknown keys {unknown}")


def _read_document(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UnbackfillableProject(f"{_FILENAME} does not parse as JSON: {exc}") from exc
    except UnicodeDecodeError as exc:
        # A file hand-edited in a legacy Windows codepage. Named here like every other
        # refusal, rather than escaping as a bare decode error with no project in it.
        raise UnbackfillableProject(f"{_FILENAME} is not UTF-8 text: {exc}") from exc
    except OSError as exc:
        raise UnbackfillableProject(f"{_FILENAME} cannot be read: {exc}") from exc
    if not isinstance(document, dict):
        raise UnbackfillableProject(
            f"{_FILENAME} holds a JSON {type(document).__name__}, not an object"
        )
    return document


def _stored_record(connection: Connection, name: str) -> dict[str, Any] | None:
    row = connection.exec_driver_sql(
        "SELECT data FROM documents WHERE collection=? AND id=?", (_COLLECTION, name)
    ).fetchone()
    if row is None:
        return None
    try:
        stored = json.loads(row[0])
    except json.JSONDecodeError as exc:
        raise UnbackfillableProject(f"its stored record is not JSON: {exc}") from exc
    if not isinstance(stored, dict):
        raise UnbackfillableProject("its stored record is not a JSON object")
    return stored

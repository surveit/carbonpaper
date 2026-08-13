"""every llm_transform names the model that answers it

Revision ID: 0013
Revises: 0012
"""
from __future__ import annotations

import json
from typing import Any

from alembic import op

from app.runtime.options import DEFAULT_MODEL
from app.services.workspace import configure_projects_dir_from_env, projects_dir
from scripts.llm_model import stamp_llm_model
from scripts.migrate_compiled_stage_files import rewrite_stale_stage_files

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

# `llm.model` is now required on LLMConfig, so a stored stage that names none loads
# nowhere. What it gets is DEFAULT_MODEL — the value the runtime resolved for exactly
# these stages, so the stamp records the model those rows were produced by rather than
# picking one. Run manifests are deliberately NOT touched: a past run holds no record of
# which model answered, and `not recorded` is the true reading of that.
#
# This revision sweeps BOTH homes of a stage spec. The document store is the obvious
# one; a project's WORKING COPY under <projects_dir>/<project>/compiled/ carries the same
# specs on disk, and 0004–0012 each left that half to a script an operator had to
# remember, which is how projects came to be stored in a shape the app cannot load. The
# container start runs `alembic upgrade head` on the machine that owns the volume, with
# CARBON_PAPER_PROJECTS_DIR set, so this is the one place both halves are reachable at
# once. An unreadable compiled file raises out of the survey before anything is written,
# which fails the boot rather than half-migrating the volume.
_COLLECTIONS = ("workflow_version", "draft")


def upgrade() -> None:
    connection = op.get_bind()
    for collection in _COLLECTIONS:
        rows = connection.exec_driver_sql(
            "SELECT id, data FROM documents WHERE collection=?", (collection,)
        ).fetchall()
        for doc_id, data in rows:
            document = json.loads(data)
            if not _stamp_document(document):
                continue
            connection.exec_driver_sql(
                "UPDATE documents SET data=? WHERE collection=? AND id=?",
                (json.dumps(document), collection, str(doc_id)),
            )
    _stamp_compiled_working_copies()


def downgrade() -> None:
    raise NotImplementedError(
        "0013 is not reversible: the stamped model is the one the stage ran on, so "
        "removing it again would discard the only record of what produced those rows"
    )


def _stamp_document(document: Any) -> bool:
    stages = document.get("stages") if isinstance(document, dict) else None
    if not isinstance(stages, list):
        return False
    stamped = [_stamp_spec(stage) for stage in stages]
    return any(stamped)


def _stamp_compiled_working_copies() -> None:
    configure_projects_dir_from_env()
    root = projects_dir()
    if not root.is_dir():
        print(f"0013: no projects root at {root}, so no working copy to stamp")
        return
    rewrite_stale_stage_files(root, _stamp_spec, apply=True)


def _stamp_spec(spec: Any) -> bool:
    return isinstance(spec, dict) and stamp_llm_model(spec, DEFAULT_MODEL)

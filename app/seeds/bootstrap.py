"""Store + projects-root wiring for the standalone `python -m app.seeds` CLI,
which has neither app.main's lifespan nor the test fixtures. Both halves are
owned elsewhere — the store by app.core.store_config, the projects root by
app.services.workspace — and re-exported here so the CLI has one import for its
whole composition root."""
from __future__ import annotations

from app.core.store_config import (
    configure_default_document_store as configure_default_document_store,
)
from app.services.workspace import configure_projects_dir_from_env as configure_projects_dir_from_env

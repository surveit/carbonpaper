"""Active-Record model definitions: PersistedModel subclasses that ARE the
stored document (own collection, own id), as distinct from the pure,
storage-free contracts in the modules above this package (schema, stage,
workflow, …). Each record embeds pure contracts (e.g. WorkflowVersion embeds
Stage) and imports PersistedModel from app.core.persistence — the one
category of module under app.core.models allowed to reach the storage base;
see the import-linter contracts in pyproject.toml.

Split across modules:
  - agent_session.py    — AgentSession, one chat session's metadata + transcript
  - workflow_version.py — WorkflowVersion, a frozen workflow snapshot
  - draft.py             — Draft, a disposable in-progress workflow edit

Each record is wrapped by an owning service (app.core.agent.store,
app.services.versioning, app.services.drafts respectively), which imports its
record from the modules here rather than defining it, so callers of that
service see no change.
"""
from app.core.models.records.agent_session import AgentSession
from app.core.models.records.draft import Draft
from app.core.models.records.workflow_version import WorkflowVersion

__all__ = ["AgentSession", "Draft", "WorkflowVersion"]

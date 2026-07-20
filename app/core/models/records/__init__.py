"""Active-Record model definitions: PersistedModel subclasses that ARE the
stored document (own collection, own id), as distinct from the pure,
storage-free contracts in the modules above this package (schema, stage,
workflow, …). Each record embeds pure contracts (e.g. WorkflowVersion embeds
Stage) and imports PersistedModel from app.core.persistence — the one
category of module under app.core.models allowed to reach the storage base;
see the import-linter contracts in pyproject.toml.

Split across modules:
  - workflow_version.py — WorkflowVersion, a frozen workflow snapshot
  - workflow_run.py      — WorkflowRun, one run's manifest
  - agent_session.py     — AgentSession, one chat session's metadata + transcript

Each owning service (app.services.versioning, app.services.run_store,
app.core.agent.store) imports its record from the modules here rather than
defining it, so callers of those services see no change.
"""
from app.core.models.records.agent_session import AgentSession
from app.core.models.records.workflow_run import WorkflowRun
from app.core.models.records.workflow_version import WorkflowVersion

__all__ = ["AgentSession", "WorkflowRun", "WorkflowVersion"]

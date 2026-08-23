from __future__ import annotations

from typing import ClassVar

from app.core.record import PersistedModel, PersistenceScope


class CodeExecutionApproval(PersistedModel):
    collection: ClassVar[str] = "code_execution_approval"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    # One record per approved project, found on this FIELD — the id stays an opaque uuid.
    project_id: str
    approved_at: str
    # In the asker's words, so whoever revokes later sees what they agreed to.
    reason: str

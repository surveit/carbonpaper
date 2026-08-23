"""Whether a project's owner has approved unsandboxed code execution, and when.

Its own record rather than a field on `Project`: app.services.project imports
app.services.stage_edit, which is where the approval is enforced, so a field
there would close an import cycle.
"""
from __future__ import annotations


from app.core.persistence import now_iso
from app.models.records.code_approval import CodeExecutionApproval

# What the owner is told before they answer. Rendered by every surface that asks
# — the tool that requests it and the page that grants it — so the warning a
# person agrees to is the same text wherever they meet it.
CODE_EXECUTION_WARNING = (
    "Carbon Paper is not built for arbitrary code execution. A Python step runs on the "
    "machine hosting this project with its permissions: it can read files, reach the "
    "network and install packages, and nothing here inspects what it does. It also "
    "reshapes the table opaquely, so a trace stops at it — a figure published downstream "
    "cannot be walked back to the rows behind it."
)


def has_code_execution_approval(project_id: str) -> bool:
    return read_code_execution_approval(project_id) is not None


def read_code_execution_approval(project_id: str) -> CodeExecutionApproval | None:
    return next(iter(CodeExecutionApproval.find(project_id=project_id)), None)


def approve_code_execution(project_id: str, reason: str) -> CodeExecutionApproval:
    """Records an answer the owner has already given. Never call it to ASK."""
    if not reason.strip():
        raise ValueError(
            "approving code execution needs the reason the owner was asked for it — "
            "what the step will do, and why no declared stage fits"
        )
    standing = read_code_execution_approval(project_id)
    if standing is not None:
        return standing
    record = CodeExecutionApproval(
        project_id=project_id, approved_at=now_iso(), reason=reason.strip()
    )
    record.save()
    return record


def withdraw_code_execution_approval(project_id: str) -> None:
    """Stages already stored keep running — this only stops NEW ones being written."""
    standing = read_code_execution_approval(project_id)
    if standing is not None:
        CodeExecutionApproval.delete(standing.id)

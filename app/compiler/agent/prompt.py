"""The editing agent's system prompt: a fixed instruction plus a rendered catalog
of the stage types it can build (so it can author a valid stage without a lookup
tool). The agent learns which project it edits at runtime via get_current_project,
so the prompt names no specific project."""

from __future__ import annotations

from app.models import NODE_TYPES

_SYSTEM_PROMPT = (
    "You help a journalist author and refine a project — a workflow of typed "
    "stages. Call get_current_project FIRST and pass its value as the `project_id` "
    "argument to every other tool. Read before you edit (describe_workflow, "
    "read_stage). Prefer small, targeted changes: edit_stage and add_stage. Every "
    "edit is validated and lands as UNREVIEWED (amber) for a human to approve — you "
    "cannot approve nodes, and you have no way to save a version (that is the "
    "human's action). compile_workflow REBUILDS the entire workflow from the "
    "conversation so far (a full reset): use it only when the user explicitly asks "
    "to rebuild from scratch, warn them first that it replaces everything and takes "
    "a few minutes, and pass confirm_overwrite=True if any node carries review work "
    "(it snapshots first). Never invent a column, source, model, or value — if you "
    "lack it, ask."
)


def _stage_type_catalog() -> str:
    """The stage-type contract rendered for the system prompt: every type, its
    handle block, that handle's required keys, and whether it takes inputs — so the
    agent can build a valid stage without a lookup tool."""
    lines = ["The stage types you can use (type — handle block; required keys; inputs):"]
    for stage_type, spec in NODE_TYPES.items():
        required = ", ".join(spec.get("required", [])) or "none"
        takes = "takes inputs" if spec.get("requires_inputs") else "no inputs"
        lines.append(f"- {stage_type} — handle `{spec['handle']}`; required: {required}; {takes}")
    return "\n".join(lines)


EDITING_SYSTEM_PROMPT = _SYSTEM_PROMPT + "\n\n" + _stage_type_catalog()

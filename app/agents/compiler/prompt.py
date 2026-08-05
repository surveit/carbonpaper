"""The editing agent's system prompt: a fixed instruction, the shared gated
authoring lifecycle (research, signed-off plan, build, smoke run before full),
the shared rule on declaring an enum from the data itself, and a rendered catalog of
the stage types it can build. The agent learns which project it edits at runtime
via get_current_project, so the prompt names no specific project."""

from __future__ import annotations

from app.models.authoring_lifecycle_note import AUTHORING_LIFECYCLE_GUIDANCE
from app.models.enum_from_data_note import ENUM_FROM_DATA_GUIDANCE
from app.models.stages.node_types import NODE_TYPES
from app.models.stages.signature import SIGNATURE_CONTRACT_NOTE

_SYSTEM_PROMPT = (
    "You help a journalist author and refine a project — a workflow of typed "
    "stages. Call get_current_project FIRST and pass its value as the `project_id` "
    "argument to every other tool. Read before you edit (describe_workflow, "
    "read_stage). Prefer small, targeted changes: edit_stage, add_stage and "
    "remove_stage (refused while another stage still inputs from the one you "
    "remove). Every "
    "edit is validated and lands as UNREVIEWED for a human to approve — you "
    "cannot approve nodes. Never invent a column, source, model, or value — if you "
    "lack it, ask.\n\n"
    "For a multi-stage restructure, work in a scratch copy instead of editing live: "
    "create_draft (pass from_version to seed it from an existing version's stages; "
    "omit it to start empty), then iterate with set_draft_stage / remove_draft_stage "
    "— invalid intermediate states are fine there, and read_draft shows what still "
    "blocks saving. When the proposal is finished, save_version once, with a message "
    "for the human reviewer explaining what changed and why. The resulting version "
    "is born UNPUBLISHED: only a human publishes it, and runs execute published "
    "versions only. A workflow does not explain itself, so once it needs the human to "
    "understand it before they act on it, call write_review_guide for the version "
    "save_version returned: an ordered walkthrough, in the methodology's own terms, "
    "saying what each part does and what a reviewer should check. For a "
    "single-field tweak to the live workflow, edit_stage remains the direct path."
)


def _stage_type_catalog() -> str:
    """Every NodeTypeSpec rendered so the agent can build a valid stage without a lookup tool."""
    lines = ["The stage types you can use:", SIGNATURE_CONTRACT_NOTE]
    for stage_type, spec in NODE_TYPES.items():
        blocks = ", ".join(f"`{b}`" for b in spec.blocks)
        required = ", ".join(spec.required) or "none"
        takes = "takes inputs" if spec.requires_inputs else "no inputs"
        lines.append(f"- {stage_type} — {spec.summary}")
        lines.append(
            f"    blocks {blocks}; required: {required}; {takes}; "
            f"signature form: {spec.signature_form}"
        )
        lines.append(f"    note: {spec.notes}")
    return "\n".join(lines)


EDITING_SYSTEM_PROMPT = (
    _SYSTEM_PROMPT + "\n\n" + AUTHORING_LIFECYCLE_GUIDANCE
    + "\n\n" + ENUM_FROM_DATA_GUIDANCE
    + "\n\n" + _stage_type_catalog()
)

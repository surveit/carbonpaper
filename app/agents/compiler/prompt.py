"""The editing agent's system prompt, assembled in seven sections: role, concepts,
how it works, the authoring lifecycle, the tightly-constrained-input rule, the stage
anatomy every type shares, and the per-type catalog. The agent learns which project it
edits at runtime, so the prompt names no specific project."""

from __future__ import annotations

from app.tools.prompt_fragments import (
    FILES_NOTE,
    HANDOVER_BARS_NOTE,
    HOW_YOU_WORK_NOTE,
    REVIEW_GUIDE_NOTE,
)
from app.models.authoring_lifecycle_note import AUTHORING_LIFECYCLE_GUIDANCE
from app.tools.prompt_fragments import ENUM_FROM_DATA_GUIDANCE
from app.tools.prompt_fragments import CONCEPTS_NOTE, ROLE_NOTE
from app.tools.prompt_fragments import render_stage_anatomy, render_type_catalog
from app.models.stages.code import (
    CODE_CORNER_CASES_CONTRACT_NOTE,
    CODE_SUMMARY_CONTRACT_NOTE,
)
from app.models.stages.stage_types import AUTHORABLE_CODE_CARRYING_TYPES
from app.models.stages.signature import SIGNATURE_CONTRACT_NOTE
from app.tools.prompt_fragments import WORKED_STAGE_EXAMPLE

_DRAFTS = """\
For a multi-stage restructure, work in a scratch copy instead of editing live:
create_draft (pass from_version to seed it from an existing version's stages; omit it to
start empty), then iterate with set_draft_stage / remove_draft_stage — invalid
intermediate states are fine there, and read_draft shows what still blocks saving.

When the proposal is finished, save_version once, with a message for the human reviewer
explaining what changed and why."""


_ATTACHING = """\
Nothing here moves bytes, and this conversation has a paperclip: the person attaches a
file to a message and the attachment line names its sha256, which is the one you profile
and bind. A file described to you in words is a file you do not have — ask for it on the
clip rather than for a path, which names nothing this app can open."""


def build_editing_system_prompt() -> str:
    return "\n\n".join([
        ROLE_NOTE,
        CONCEPTS_NOTE,
        HOW_YOU_WORK_NOTE,
        _DRAFTS,
        REVIEW_GUIDE_NOTE,
        HANDOVER_BARS_NOTE,
        f"# Files\n{FILES_NOTE}\n\n{_ATTACHING}",
        f"# Project lifecycle\n{AUTHORING_LIFECYCLE_GUIDANCE}",
        f"# Rules for workflows\n\n## Constrain inputs as tightly as possible\n"
        f"{ENUM_FROM_DATA_GUIDANCE}",
        render_stage_anatomy_section(),
        render_stage_type_catalog(),
    ])


def render_stage_anatomy_section() -> str:
    governed = ", ".join(f"`{name}`" for name in AUTHORABLE_CODE_CARRYING_TYPES)
    return "\n\n".join([
        "# Anatomy of a stage",
        render_stage_anatomy(),
        SIGNATURE_CONTRACT_NOTE,
        f"## Describing authored code (applies to: {governed})\n"
        f"{CODE_SUMMARY_CONTRACT_NOTE}\n{CODE_CORNER_CASES_CONTRACT_NOTE}",
        f"## A stage, whole\n{WORKED_STAGE_EXAMPLE}",
    ])


def render_stage_type_catalog() -> str:
    return "# The stage types you can use\n" + render_type_catalog()


EDITING_SYSTEM_PROMPT = build_editing_system_prompt()

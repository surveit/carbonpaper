"""The editing agent's system prompt, assembled in seven sections: role, concepts,
how it works, the authoring lifecycle, the tightly-constrained-input rule, the stage
anatomy every type shares, and the per-type catalog. The agent learns which project it
edits at runtime, so the prompt names no specific project."""

from __future__ import annotations

from app.models.authoring_lifecycle_note import AUTHORING_LIFECYCLE_GUIDANCE
from app.models.enum_from_data_note import ENUM_FROM_DATA_GUIDANCE
from app.models.product_note import CONCEPTS_NOTE, ROLE_NOTE
from app.models.stages.anatomy_note import render_stage_anatomy
from app.models.stages.code import (
    CODE_CORNER_CASES_CONTRACT_NOTE,
    CODE_SUMMARY_CONTRACT_NOTE,
)
from app.models.stages.node_types import (
    AUTHORABLE_CODE_CARRYING_TYPES,
    AUTHORABLE_TYPES,
    CODE_CARRYING_TYPES,
)
from app.models.stages.signature import SIGNATURE_CONTRACT_NOTE
from app.models.stages.worked_example import WORKED_STAGE_EXAMPLE

_HOW_YOU_WORK = """\
# How you work
Read before you edit (describe_workflow, read_stage). Prefer small, targeted changes.
Every edit may have complex validations, so large expensive edits that result in errors
are token inefficient.

Never invent a column, source, model, or value — if you lack it, ask the user. The reason
for this rule is that an LLM invented figure will not survive the validation step, which
itself exists to ensure that the asymmetric risk of publishing something wrong is
prevented.

For a multi-stage restructure, work in a scratch copy instead of editing live:
create_draft (pass from_version to seed it from an existing version's stages; omit it to
start empty), then iterate with set_draft_stage / remove_draft_stage — invalid
intermediate states are fine there, and read_draft shows what still blocks saving.

When the proposal is finished, save_version once, with a message for the human reviewer
explaining what changed and why.

A workflow does not explain itself, so a version the human has to understand before
acting on it needs write_review_guide: an ordered walkthrough, in the methodology's own
terms, saying what each part does and what a reviewer should check. Write it in
TEST_RUN_REVIEW — after the smoke run, never straight off save_version."""


def build_editing_system_prompt() -> str:
    return "\n\n".join([
        ROLE_NOTE,
        CONCEPTS_NOTE,
        _HOW_YOU_WORK,
        f"# Project lifecycle\n{AUTHORING_LIFECYCLE_GUIDANCE}",
        f"# Rules for workflows\n\n## Constrain inputs as tightly as possible\n"
        f"{ENUM_FROM_DATA_GUIDANCE}",
        render_stage_anatomy_section(),
        render_stage_type_catalog(),
    ])


def render_stage_anatomy_section() -> str:
    """What holds for every stage, so no type's own note restates it."""
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
    """Only what is specific to one type — the anatomy above covers the rest."""
    return "\n".join([
        "# The stage types you can use",
        *[_render_stage_type(stage_type, spec)
          for stage_type, spec in AUTHORABLE_TYPES.items()],
    ])


def _render_stage_type(stage_type: str, spec: object) -> str:
    assert isinstance(spec, type(AUTHORABLE_TYPES[stage_type]))
    blocks = ", ".join(f"`{b}`" for b in spec.blocks)
    required = ", ".join(spec.required) or "none"
    takes = "takes inputs" if spec.requires_inputs else "no inputs"
    carries = " CARRIES CODE." if stage_type in CODE_CARRYING_TYPES else ""
    lines = [
        f"- {stage_type} — {spec.summary}{carries}",
        f"    blocks {blocks}; required: {required}; {takes}; "
        f"signature form: {spec.signature_form}",
    ]
    if spec.notes:
        lines.append(f"    note: {spec.notes}")
    return "\n".join(lines)


EDITING_SYSTEM_PROMPT = build_editing_system_prompt()

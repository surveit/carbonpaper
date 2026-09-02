"""The editing agent's system prompt, assembled in seven sections: role, concepts,
how it works, the authoring lifecycle, the tightly-constrained-input rule, the stage
anatomy every type shares, and the per-type catalog. The agent learns which project it
edits at runtime, so the prompt names no specific project."""

from __future__ import annotations

from app.tools.prompt_fragments import (
    AUTHORABLE_CODE_CARRYING_TYPES,
    AUTHORING_LIFECYCLE_GUIDANCE,
    CODE_CORNER_CASES_CONTRACT_NOTE,
    CODE_SUMMARY_CONTRACT_NOTE,
    CONCEPTS_NOTE,
    ENUM_FROM_DATA_GUIDANCE,
    FILTER_ON_MEANING_GUIDANCE,
    HANDOVER_BARS_NOTE,
    HOW_YOU_WORK_NOTE,
    REVIEW_GUIDE_NOTE,
    ROLE_NOTE,
    ROW_LINEAGE_PAGE_NOTE,
    SIGNATURE_CONTRACT_NOTE,
    WORKED_STAGE_EXAMPLE,
    render_stage_anatomy,
    render_type_catalog,
)

def build_editing_system_prompt() -> str:
    return "\n\n".join([
        ROLE_NOTE,
        CONCEPTS_NOTE,
        HOW_YOU_WORK_NOTE,
        REVIEW_GUIDE_NOTE,
        HANDOVER_BARS_NOTE,
        ROW_LINEAGE_PAGE_NOTE,
        f"# Project lifecycle\n{AUTHORING_LIFECYCLE_GUIDANCE}",
        f"# Rules for workflows\n\n## Constrain inputs as tightly as possible\n"
        f"{ENUM_FROM_DATA_GUIDANCE}\n\n"
        f"## Filter on the column that carries the meaning\n"
        f"{FILTER_ON_MEANING_GUIDANCE}",
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

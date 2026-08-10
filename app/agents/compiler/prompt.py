"""The editing agent's system prompt, assembled in six sections: role, concepts,
how it works, the authoring lifecycle, the tightly-constrained-input rule, and the
stage-type catalog. The agent learns which project it edits at runtime via
get_current_project, so the prompt names no specific project."""

from __future__ import annotations

from app.models.authoring_lifecycle_note import AUTHORING_LIFECYCLE_GUIDANCE
from app.models.enum_from_data_note import ENUM_FROM_DATA_GUIDANCE
from app.models.stages.code import (
    CODE_CORNER_CASES_CONTRACT_NOTE,
    CODE_SUMMARY_CONTRACT_NOTE,
)
from app.models.stages.node_types import CODE_CARRYING_TYPES, NODE_TYPES
from app.models.stages.signature import SIGNATURE_CONTRACT_NOTE

_ROLE = """\
# Role
You are an AI assistant in CarbonPaper, which exists to help non-AI engineers get
results that can pass a verification challenge. An example would be a journalist
analyzing a dataset for a single publishable number that passes fact check."""

_CONCEPTS = """\
# Concepts
1. Project — a single worked goal, e.g. analyzing AI lobbying spend. Or a repeatable
   workflow to evaluate if companies are making progress on their climate commitments.
2. Methodology — a document detailing the project's spec. This should mirror the user's
   input near verbatim, even if it makes for a poor spec. Do not invent anything that
   was not directly provided just to improve the quality of the spec.
3. Workflow — the actual set of data transform stages that runs.
4. Run — one specific instance of a set of input data being transformed by the workflow."""

_HOW_YOU_WORK = """\
# How you work
Call get_current_project FIRST and pass its value as the `project_id` argument to every
other tool. Read before you edit (describe_workflow, read_stage). Prefer small, targeted
changes: edit_stage, add_stage and remove_stage (refused while another stage still inputs
from the one you remove). Every edit may have complex validations, so large expensive
edits that result in errors are token inefficient. Every edit lands as UNREVIEWED for a
human to approve — you cannot approve stages.

Never invent a column, source, model, or value — if you lack it, ask the user. The reason
for this rule is that an LLM invented figure will not survive the validation step, which
itself exists to ensure that the asymmetric risk of publishing something wrong is
prevented.

For a multi-stage restructure, work in a scratch copy instead of editing live:
create_draft (pass from_version to seed it from an existing version's stages; omit it to
start empty), then iterate with set_draft_stage / remove_draft_stage — invalid
intermediate states are fine there, and read_draft shows what still blocks saving.

When the proposal is finished, save_version once, with a message for the human reviewer
explaining what changed and why. The resulting version is born UNPUBLISHED: only a human
publishes it, and runs execute published versions only. For a single-field tweak to the
live workflow, edit_stage remains the direct path.

A workflow does not explain itself, so a version the human has to understand before
acting on it needs write_review_guide: an ordered walkthrough, in the methodology's own
terms, saying what each part does and what a reviewer should check. Write it in
TEST_RUN_REVIEW — after the smoke run, never straight off save_version."""


def build_editing_system_prompt() -> str:
    return "\n\n".join([
        _ROLE,
        _CONCEPTS,
        _HOW_YOU_WORK,
        f"# Project lifecycle\n{AUTHORING_LIFECYCLE_GUIDANCE}",
        f"# Rules for workflows\n\n## Constrain inputs as tightly as possible\n"
        f"{ENUM_FROM_DATA_GUIDANCE}",
        render_stage_type_catalog(),
    ])


def render_stage_type_catalog() -> str:
    """Every NodeTypeSpec, under the rules that govern whole groups of them."""
    return "\n".join([
        "# Workflow data model details",
        "",
        SIGNATURE_CONTRACT_NOTE,
        "",
        _authored_code_rules(),
        "",
        "The stage types you can use:",
        *[_render_stage_type(stage_type, spec) for stage_type, spec in NODE_TYPES.items()],
    ])


def _authored_code_rules() -> str:
    """The description contract, stated once for the types marked CARRIES CODE below."""
    governed = ", ".join(f"`{name}`" for name in CODE_CARRYING_TYPES)
    return "\n".join([
        f"## Describing authored code (applies to: {governed})",
        CODE_SUMMARY_CONTRACT_NOTE,
        CODE_CORNER_CASES_CONTRACT_NOTE,
    ])


def _render_stage_type(stage_type: str, spec: object) -> str:
    assert isinstance(spec, type(NODE_TYPES[stage_type]))
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

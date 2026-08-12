"""The MCP server's INSTRUCTIONS: the shared authoring guidance every surface
carries, plus the tool walkthrough only this surface needs. Held apart from
app.mcp.server so that module imports one thing here, not every prompt-content
module (app.mcp.server sits at the import fan-out ceiling)."""
from __future__ import annotations

import textwrap

from app.tools.prompt_fragments import (
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


def _render_stage_type_constraints() -> str:
    governed = ", ".join(f"`{name}`" for name in AUTHORABLE_CODE_CARRYING_TYPES)
    return "\n".join([
        textwrap.fill(render_stage_anatomy(), width=88),
        "",
        textwrap.fill(SIGNATURE_CONTRACT_NOTE, width=88),
        "",
        f"Describing authored code (applies to: {governed}):",
        textwrap.fill(CODE_SUMMARY_CONTRACT_NOTE, width=88),
        textwrap.fill(CODE_CORNER_CASES_CONTRACT_NOTE, width=88),
        "",
        "A stage, whole:",
        WORKED_STAGE_EXAMPLE,
        "",
        render_type_catalog(),
    ])


_STAGE_TYPE_CONSTRAINTS = _render_stage_type_constraints()

INSTRUCTIONS = f"""\
{ROLE_NOTE}
YOU author the workflow through these tools. Every stage is validated against the whole
graph before it is stored.

{CONCEPTS_NOTE}

{HOW_YOU_WORK_NOTE}

# The lifecycle every project follows
{AUTHORING_LIFECYCLE_GUIDANCE}
(Here, a limited run is run_workflow_test's `limit`/`offset` slice; a full run is
run_workflow.)

{ENUM_FROM_DATA_GUIDANCE}
(Here: save_version, then run_workflow_test(stage_ids=["<the input stage id>"],
limit=null) — a named source stage EXECUTES, and a null limit is the whole bound
file — then profile_stage_output_data_range on what it wrote. edit_stage tightens
the schema afterwards.)

# Your tools, by phase
Each tool's own description says how it behaves; this says WHEN. Start by calling
create_project(name, document) — the methodology prose becomes the project's source of
record, and it returns the project record, whose `id` every other tool takes.

  RESEARCH   read_data_model, describe_workflow, read_stage, get_project_status
             run_workflow_test over a few rows, then
             profile_stage_output_data_range, to see what the data really holds,
             and read_stage_output_rows when the question is about a ROW
  TERMS      read_terms to see what is agreed, then write_terms once the user has
             agreed the rest. Every later phase writes in those words.
  PLANNING   (no tools — this is where you ask the user)
  BUILD      add_stage, edit_stage, remove_stage, then generate_stage_tests and
             loop edit_stage -> run_stage_tests until they pass. Still BUILD.
  TEST_RUN   save_version, then run_workflow_test against it. Read that output.
             What it finds sends you back to BUILD, and you run again.
  REVIEW     write_review_guide for that version (read_review_guide shows what a
             version already carries), then report_compiler_warnings,
             then hand over: the version, its guide, the test run it was written
             against, and the warnings still open.

generate_data_model(project_id) runs in the background — poll get_project_status until
schemas appear. The HUMAN then approves the data model in the web UI; no tool approves
it. A run executes a stored version, and run_workflow(project_id, version_id?) is the
full one — get_run_status(project_id, run_id) follows it to its outcome. Publishing is a
human's mark that they have looked at a version; it does not gate what a run may execute.

{REVIEW_GUIDE_NOTE}

{HANDOVER_BARS_NOTE}

# Constraints
{_STAGE_TYPE_CONSTRAINTS}

list_projects() names the projects that already have an authored workflow;
get_project_status(project_id) is the full snapshot of any one project."""

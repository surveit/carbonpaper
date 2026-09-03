"""The MCP server's INSTRUCTIONS: the shared authoring guidance every surface
carries, plus the tool walkthrough only this surface needs. Held apart from
app.mcp.server so that module imports one thing here, not every prompt-content
module (app.mcp.server sits at the import fan-out ceiling)."""
from __future__ import annotations

import textwrap

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
    SIGNATURE_CONTRACT_NOTE,
    WORKED_STAGE_EXAMPLE,
    render_stage_anatomy,
    render_type_catalog,
)


def _render_stage_type_constraints() -> str:
    governed = ", ".join(f"`{name}`" for name in AUTHORABLE_CODE_CARRYING_TYPES)
    return "\n".join([
        # Not re-wrapped: the anatomy carries a grain table whose indentation is its
        # structure, and refilling it also hyphen-splits words across lines.
        render_stage_anatomy(),
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
file — then profile_stage_output_data_range on what it wrote. edit_stages tightens
the schema afterwards.)

{FILTER_ON_MEANING_GUIDANCE}

# Your tools, by phase
Each tool's own description says how it behaves; this says WHEN. Start by calling
create_project(name, document) — the methodology prose becomes the project's source of
record, and it returns the project record, whose `id` every other tool takes.

  RESEARCH   read_workflow_summary, read_stage, get_project_status, list_runs
             for what this project has already run,
             run_workflow_test over a few rows, then
             profile_stage_output_data_range, to see what the data really holds,
             and read_stage_output_rows when the question is about a ROW
  TERMS      read_terms to see what is agreed, then write_terms once the user has
             agreed the rest. Every later phase writes in those words.
             read_claim_shapes to see what is already promised.
  PLANNING   write_claim_shapes once you and the user agree what the workflow is
             for. A shape is a figure this project promises to report, so the
             shapes ARE the expected outcome — everything you build after this
             exists to fulfil them, and a workflow that cannot is not done.
  BUILD      add_stage, edit_stages, delete_stage, then generate_stage_tests and
             loop edit_stages -> run_stage_tests until they pass. Still BUILD.
             If a write is refused because the step needs unsandboxed Python,
             that refusal names what to try instead. Only if nothing fits, put
             it to the user in their words, wait for their answer, and call
             approve_code_execution once they have said yes.
  TEST_RUN   save_version, then run_workflow_test against it. Read that output.
             What it finds sends you back to BUILD, and you run again.
  REVIEW     write_review_guide for that version (read_review_guide shows what a
             version already carries), then report_compiler_warnings,
             then hand over: the version, its guide, the test run it was written
             against, and the warnings still open.

A run executes a stored version, and run_workflow(project_id, version_id?) is the
full one — get_run_status(project_id, run_id) follows it to its outcome, and list_runs
names the runs someone else started. Publishing is a
human's mark that they have looked at a version; it does not gate what a run may execute.

An input step reads a file the project holds. list_files(project_id) is what it holds, and
run_workflow's `files` binds one to a step by the file_id it gives. To upload a file, POST
to the returned file_upload_url. list_files(null) is the files in no project, and
move_file_to_project puts one in.

profile_file(project_id, file_id) is what that file HOLDS. Declare an input step's schema
from it rather than from asking someone to describe their own file — they answer from
memory, and the profile is the file. It reads only a file the project holds, so a listed
file in no project is moved in first. On an xlsx, call survey_workbook first: it names
the sheets and shows each one's first cells, which is where you read off the sheet, the
header row and the first column that profile_file and the input step both then take.

{REVIEW_GUIDE_NOTE}

{HANDOVER_BARS_NOTE}

# Constraints
{_STAGE_TYPE_CONSTRAINTS}

list_projects() names the projects that already have an authored workflow;
get_project_status(project_id) is the full snapshot of any one project."""

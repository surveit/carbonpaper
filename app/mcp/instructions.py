"""The MCP server's INSTRUCTIONS: the shared authoring guidance every surface
carries, plus the tool walkthrough only this surface needs. Held apart from
app.mcp.server so that module imports one thing here, not every prompt-content
module (app.mcp.server sits at the import fan-out ceiling)."""
from __future__ import annotations

import textwrap

from app.models.authoring_lifecycle_note import AUTHORING_LIFECYCLE_GUIDANCE
from app.models.enum_from_data_note import ENUM_FROM_DATA_GUIDANCE
from app.models.product_note import CONCEPTS_NOTE, ROLE_NOTE
from app.models.stages.anatomy_note import render_stage_anatomy, render_type_catalog
from app.models.stages.code import (
    CODE_CORNER_CASES_CONTRACT_NOTE,
    CODE_SUMMARY_CONTRACT_NOTE,
)
from app.models.stages.node_types import AUTHORABLE_CODE_CARRYING_TYPES
from app.models.stages.signature import SIGNATURE_CONTRACT_NOTE
from app.models.stages.worked_example import WORKED_STAGE_EXAMPLE


def _render_node_type_constraints() -> str:
    """From the shared specs, so the two authoring prompts cannot drift apart."""
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


_NODE_TYPE_CONSTRAINTS = _render_node_type_constraints()

INSTRUCTIONS = f"""\
{ROLE_NOTE}
YOU author the workflow through these tools. Every stage is validated against the whole
graph before it is stored.

{CONCEPTS_NOTE}

# The lifecycle every project follows
{AUTHORING_LIFECYCLE_GUIDANCE}
(Here, a limited run is run_workflow_test's `limit`/`offset` slice; a full run is
run_workflow.)

{ENUM_FROM_DATA_GUIDANCE}
(Here: save_version, then run_workflow_test(stage_ids=["<the input stage id>"],
limit=null) — a named source stage EXECUTES, and a null limit is the whole bound
file — then profile_stage_output_data_range on what it wrote. edit_stage tightens
the schema afterwards.)

# Setup
1. create_project(name, document) — the methodology prose becomes the project's source
   of record. Returns the project_id every other tool takes.
2. generate_data_model(project_id) — generates the named schemas from the document. Runs in
   the background; poll get_project_status until schemas appear.
3. The HUMAN approves the data model in the web UI. No tool approves it.

# BUILD — authoring the workflow
4. Read the methodology document and read_data_model(project_id). The approved schemas are
   the vocabulary the stages carry.
5. Plan the stages, then add_stage(project_id, stages) them — `stages` is a LIST, so send
   every stage you are ready to author in ONE call rather than one per call. Order does not
   matter: they are sorted by the `inputs` they declare, and an input may name a stage in
   the same call or one already in the workflow. Stages that validate are stored even if
   another in the batch fails; the result's added/failed/skipped says which is which. The
   workflow starts with an input_data stage that reads the source and takes no inputs.
6. What an upstream stage's `signature` promises is what flows down the edge. A stage's
   MANDATORY declared input schema is usually that verbatim; it differs when the stage reads
   only part of what upstream emits. Either way it must be a subset the upstream can satisfy.
7. As the graph grows: describe_workflow(project_id) for the shape (ids, types, inputs,
   review state), read_stage(project_id, stage_id) for one stage in full,
   edit_stage(project_id, stage_id, changes_json) to change only the fields you name (a
   JSON Merge Patch), remove_stage(project_id, stage_id) to undo a stage you added
   (refused while another stage still lists it in `inputs`).

Only a human publishes a version, and a run executes a published one.

# BUILD — per-stage tests
8. Once a python-transform stage exists, generate_stage_tests writes its tests from the
   methodology; then loop edit_stage → run_stage_tests until they pass — before any run.

# TEST_RUN — smoke before full
9. Runs execute a stored version; save_version(project_id, message) creates one, then
   run_workflow_test(project_id, limit, version_id?, stage_ids?, offset?) executes it —
   published or not — over `limit` rows of the real source, as a run marked is_test_run;
   profile_stage_output_data_range then profiles what a stage of it wrote. READ THAT
   OUTPUT YOURSELF: what it finds sends you back to the build, and then you run again.
   run_workflow(project_id, version_id?) starts a real full run and returns a run_id, and
   get_run_status(project_id, run_id) follows it to its outcome. Publishing is human-only.

# TEST_RUN_REVIEW — the review guide, and why it exists
A workflow you author is not self-explaining. The human who owns the methodology has to
decide whether it does what they meant — and they read the stage graph, not the code. The
review guide is the prose that makes that decision possible: an ordered walkthrough,
each step naming the stages it covers and saying what a reviewer should check.

10. write_review_guide(project_id, version_id, guide) — the LAST thing you author, after
   the smoke run. Nothing generates one and nothing seeds one; you write it from a blank
   page. read_review_guide shows what a version already carries.
   Write it FOR the methodology's owner, not a programmer: use the document's terms of
   art, wrap column names in `backticks`, and say what could be quietly wrong rather than
   restating the stage names and order the page already shows.
11. report_compiler_warnings(project_id), then hand over together: the version, its guide,
   the test run it was written against, and the warnings still open. Which bar you are
   asking against is below.

# Finishing
report_compiler_warnings(project_id) reports what is wrong with the workflow,
including any stage whose examples do not pass. Dirty is fine while you build.

Two different things you can ask a human for, with different bars:
- A look at a smoke test — run_workflow_test, what came out of it, and the guide you wrote
  for that version. Fine with warnings outstanding; say which ones are open.
- FINAL SIGNOFF. Do not ask for this with any warning outstanding. Either clear it, or
  state plainly why that specific warning is safe to ignore here. A warning you leave
  unmentioned spends the reviewer's attention on something you already knew about.

# Constraints
{_NODE_TYPE_CONSTRAINTS}
- Never fabricate a column, source, model, or value. If the methodology does not supply it,
  leave it out and say what is missing.

list_projects() names the projects that already have an authored workflow;
get_project_status(project_id) is the full snapshot of any one project."""

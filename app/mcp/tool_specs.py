"""Every glassbox MCP tool's model-facing description, keyed by tool name.
Held here, not in a docstring, so the text the model reads is an explicit argument to
@mcp.tool rather than a side effect of how the function happens to be documented.
FastMCP derives each input schema from the signature, so these carry none."""
from __future__ import annotations

from app.core.agent.tool_spec import ToolSpec

TOOL_SPECS: dict[str, ToolSpec] = {
    "list_projects": ToolSpec(
        name="list_projects",
        description="""\
List the names of every project in the workspace that has an authored
workflow. A just-created project appears here only once its first stage has
been added — use get_project_status(project_id) to inspect one before that.""",
    ),
    "create_project": ToolSpec(
        name="create_project",
        description="""\
Create a NEW project from a methodology document (prose describing how the
investigation finds, verifies, and surfaces its claims). Writes the document
as the project's source of record. Returns the project_id (the sanitized
name). Fails loudly if the name is taken — never overwrites. Next step:
generate_data_model(project_id).""",
    ),
    "get_project_status": ToolSpec(
        name="get_project_status",
        description="""\
One project's full status snapshot: document present?, data-model state
(generating shows no schemas yet; then unapproved/approved), workflow stage
counts and review coverage, versions, runs. Poll this after generate_* to see
the result land.""",
    ),
    "generate_data_model": ToolSpec(
        name="generate_data_model",
        description="""\
Generate the project's DATA MODEL (named schemas) from its methodology
document. Starts a live generation turn in the background and returns
immediately — poll get_project_status until schemas appear, and tell the user
they can watch it stream at the returned `watch` path in the web UI. The
human then reviews/approves the data model in the web UI; the approved
schemas are the vocabulary you author the workflow's stages against.""",
    ),
    "generate_stage_tests": ToolSpec(
        name="generate_stage_tests",
        description="""\
Derive tests for one stage that can run them FROM THE METHODOLOGY. The
derivation is code-blind by construction: the deriver only ever sees the
methodology document plus the data model / stage schemas, never the stage's
code or any existing tests — so calling this right after generating or
editing the code cannot anchor the tests on the implementation (that would
assert the code equals itself). Starts a background turn and returns
immediately; on completion the derived suite REPLACES the stage's tests
wholesale. Fails loudly if the stage's type carries no runnable tests, or it
has no output schema.""",
    ),
    "run_stage_tests": ToolSpec(
        name="run_stage_tests",
        description="""\
Run a stage's authored tests against its CURRENT code and report the
result. Omit `stage_id` to run every stage that has tests, or pass one to
scope the run to that stage. Use this after regenerating code
with edit_stage to see which tests the new code fails — the report carries a
summary plus, per test, its status and any cell diffs, and lists
`untested_stages` (testable stages with no tests, a coverage gap).
This does NOT edit tests: a failing test means the code disagrees with the
frozen test, and the fix is to the code (or to re-derive via
generate_stage_tests), never to bend the test to the code.""",
    ),
    # The signoff protocol this used to restate (clear every warning first, or say
    # why one is safe to leave) is in INSTRUCTIONS' "Finishing" section already.
    "report_compiler_warnings": ToolSpec(
        name="report_compiler_warnings",
        description="""\
Every problem with this workflow: undescribed stages, descriptions no examples
check, examples that do not pass, code the review panel cannot show, and
deliberate choices (cache off, row limit) a reviewer should be told about.
`blocking` is the subset you can clear by editing the stage. This DOES run the
examples — a workflow whose examples disagree with its code is not
signed-off-able — but run_stage_tests is what tells you which case failed.""",
    ),
    "read_data_model": ToolSpec(
        name="read_data_model",
        description="""\
The project's data model: every named schema as JSON (empty list if none
generated yet).""",
    ),
    "describe_workflow": ToolSpec(
        name="describe_workflow",
        description="""\
Summarize a project's workflow: each stage's id, type, name, upstream input
ids, and review state. Read this before editing so you know the current
shape. Does not return full stage specs — use read_stage for one.""",
    ),
    "read_stage": ToolSpec(
        name="read_stage",
        description="""\
Return the JSON of one stage from the workflow. Read before editing.""",
    ),
    "edit_stage": ToolSpec(
        name="edit_stage",
        description="""\
Change specific fields of one stage. `changes_json` is a JSON object of
ONLY the fields to change (a JSON Merge Patch): {"limit": 100} sets limit;
{"llm": {"model": "opus"}} changes only llm.model and leaves the rest of the
llm block intact; {"name": null} deletes a field. Fields you do not mention
are preserved exactly. Validated first; if invalid, nothing is written and
the issues are returned. A successful edit drops the node to 'edited_stale'
for a human to re-approve — you cannot approve it yourself. You cannot
change a stage's id this way.""",
    ),
    "add_stage": ToolSpec(
        name="add_stage",
        description="""\
Create NEW stages in the workflow. `stages` is a LIST — submit every stage
you are ready to author in ONE call; a list of one is the single-stage case.
Each is a FULL stage: id (new and unique — use edit_stage to change an
existing one), name, type, the config block(s) its type requires — connector
/ llm / function / join / aggregate / queue / union / filter, and `publish`
needs BOTH its `publish` block and a `function` block — output_schema, and
inputs. read_stage on a similar existing stage shows the shape.

Order does not matter: the batch is sorted by the `inputs` each stage
declares, so a stage may name another stage in the SAME call as an input, or
one already in the workflow.

Each stage is validated against the whole workflow-so-far before it is
written: its own shape, unique ids, inputs resolving, no cycles, and edge
conformance — a column a stage declares on an input that the upstream's
output_schema does not supply is refused. The result reports every stage:

  added   — ids now in the workflow
  failed  — [{id, issues}]; that stage was NOT written, the rest still were
  skipped — [{id, because}]; not attempted, because a stage it inputs from
            failed or was itself skipped
  issues  — every failure's issues flattened, so `ok`/`issues` reads the same
            as it always has

Fix what `failed` names and re-send only the failed and skipped stages:
read_stage the named upstream, repair the declared input schema against what
that stage really outputs. A batch that cannot be ordered at all — duplicate
ids, or a cycle among the submitted stages — is refused whole, with NOTHING
written and the cycle named in `issues`.

Copying a stage from read_stage is fine: the server-owned fields it carries
(tests, eval, review, source) are dropped rather than refused, and a
`warnings` entry names the stage and the fields dropped from it. Any OTHER
unknown field is still an error — a typo'd field name never passes silently.

New nodes land 'unreviewed' for a human to approve. The FIRST stage of a
project starts its workflow — no other tool creates one.""",
    ),
    "remove_stage": ToolSpec(
        name="remove_stage",
        description="""\
Delete one stage from the workflow — the undo for a stage you added. The
workflow WITHOUT the stage is validated first: if another stage still lists it
in `inputs`, the removal is refused, nothing is deleted, and the issues are
returned (remove or repoint the downstream stage first). Removing the last
remaining stage is allowed.""",
    ),
    "save_version": ToolSpec(
        name="save_version",
        description="""\
Freeze the project's CURRENT workflow into an immutable version — the snapshot
a run or a workflow test executes. Born UNPUBLISHED: only a human publishes.

`parent_version` is the version YOU started this edit from. Supply it only when you
actually loaded that version; it is recorded verbatim as this snapshot's ancestor,
and an id naming no version of this project is refused. Omitting it is normal and
records no ancestor — nothing is inferred from what else the project has stored.

The working copy is strict-loaded first, so an invalid workflow comes back as
{ok: False, issues} and no version is written.""",
    ),
    "read_review_guide": ToolSpec(
        name="read_review_guide",
        description="""\
The review guide stored on one saved version, or null when it has none. Read
before writing so you amend it rather than replace someone's work.""",
    ),
    "write_review_guide": ToolSpec(
        name="write_review_guide",
        description="""\
Store the walkthrough a human reads to understand what this version of the
workflow does. Replaces any guide already on that version, whole.""",
    ),
    "run_workflow": ToolSpec(
        name="run_workflow",
        description="""\
Start a REAL production run of the project's published workflow and return
its `run_id` immediately — the run executes in the background. This is a run
of record: it writes a manifest under the project's runs/ dir and produces the
workflow's published artifacts. `version_id` pins a specific published version
(omit for the newest published one); an unpublished or missing version is a
loud error, never a silent fallback. Poll get_run_status(project_id, run_id)
for live progress and the final status. On a pre-run failure (nothing
published, an unbound input) returns {ok: False, error} and starts no run.""",
    ),
    "get_run_status": ToolSpec(
        name="get_run_status",
        description="""\
The current manifest of one production run as a dict: its overall status
(running / ok / errors / halted), per-stage statuses, and run metadata. Poll
this after run_workflow to follow progress and see the outcome. An unknown or
expired run_id returns {ok: False, error} rather than a fabricated status.""",
    ),
    "run_workflow_test": ToolSpec(
        name="run_workflow_test",
        description="""\
Run a workflow test, so an author can watch the pipeline execute on real
data before publishing. It IS a real run — same `runs/` dir, manifest, and
trace/view routes as run_workflow's — and differs from run_workflow on
exactly five axes:

1. VERSION: any stored version, published or not (run_workflow pins a
   published one). Omit `version_id` for the newest stored.
2. SOURCE: the `limit` rows from `offset` of the workflow's bound source,
   injected (run_workflow reads the whole source through input_data).
3. EXECUTION: synchronous — this returns when the run is done (run_workflow
   returns a run_id immediately and executes on a background thread).
4. REVIEW QUEUE: a human_review_queue stage auto-approves every row in
   memory (run_workflow halts there and waits for a human).
5. STAGE CACHE: read-only — it may replay a workflow run's cached results
   but records none of its own, so it cannot affect a later run.

Marked `is_test_run` on the manifest, so it never counts as the project's
latest run. Returns the verdict {ok, run_id, version_id, stages_run, error}:
`ok` False on any stage error, with `error` naming what failed; poll
get_run_status(project_id, run_id) for the same live/final manifest
run_workflow exposes. A project with no stored version is a loud error.""",
    ),
}

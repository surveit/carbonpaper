"""Every tool description both authoring surfaces read, keyed by tool name.
The MCP server and the editing agent expose overlapping tools; holding the
prose once is what stops the two drifting. `save_version` is the one name meaning two
different operations — see SAVE_VERSION_* below."""
from __future__ import annotations

from app.core.agent.tool_spec import ToolSpec

TOOL_SPECS: dict[str, ToolSpec] = {
    "add_stage": ToolSpec(
        name="add_stage",
        description="""\
Create NEW stages in the workflow. `stages` is a LIST — submit every stage
you are ready to author in ONE call; a list of one is the single-stage case.
Each is a FULL stage, as the anatomy describes one. Its `id` is new, unique,
and the stage's ONLY name — every surface shows it, so name the step well;
use edit_stage to change an existing one. `publish` is the one type needing
TWO blocks: its own and a `function`. There is no output_schema to send —
the stage's output IS what its signature promises.

Order does not matter: the batch is sorted by the `inputs` each stage
declares, so a stage may name another stage in the SAME call as an input, or
one already in the workflow.

Each stage is validated against the whole workflow-so-far before it is
written: its own shape, unique ids, inputs resolving, no cycles, and edge
conformance — a column a stage declares on an input that the upstream's
resolved output does not supply is refused. The result reports every stage:

  added   — ids now in the workflow
  failed  — [{id, issues}]; that stage was NOT written, the rest still were
  skipped — [{id, because}]; not attempted, because a stage it inputs from
            failed or was itself skipped

Re-send only the failed and skipped stages. A batch that cannot be ordered at
all — duplicate ids, or a cycle among the submitted stages — is refused whole,
with NOTHING written and the cycle named in `issues`.

Copying a stage from read_stage is fine: the server-owned fields it carries
(tests, eval, review, source) are dropped rather than refused, and a
`warnings` entry names the stage and the fields dropped from it. Any OTHER
unknown field is still an error — a typo'd field name never passes silently.

The FIRST stage of a project starts its workflow — no other tool creates one.""",
    ),
    "create_draft": ToolSpec(
        name="create_draft",
        description="""\
Start a DRAFT: a disposable scratch copy of workflow stages you edit
freely and later freeze with save_version. Each stage you set must be
individually valid, but the WORKFLOW may stay incomplete mid-build (e.g.
a stage whose input references one you have not added yet) until you
save. Pass from_version to seed it from an existing version's stages;
omit it to start empty. Returns the draft, whose `id` (a word triplet
like brisk-otter-lamp) you pass to every draft tool. Drafts are
expendable — if one is lost, start a new one.""",
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
    "describe_workflow": ToolSpec(
        name="describe_workflow",
        description="""\
Summarize a project's workflow: each stage's id, type, description, upstream
input ids, and review state. Read this before editing so you know the current
shape. Does not return full stage specs — use read_stage for one.""",
    ),
    "edit_stage": ToolSpec(
        name="edit_stage",
        description="""\
Change specific fields of one stage. `changes_json` is a JSON object of
ONLY the fields to change (a JSON Merge Patch): {"limit": 100} sets limit;
{"llm": {"model": "claude-opus-5"}} changes only llm.model and leaves the rest of the
llm block intact; {"limit": null} deletes a field. Fields you do not mention
are preserved exactly. Validated first; if invalid, nothing is written and
the issues are returned. You cannot change a stage's id this way.""",
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
Generate tests for one stage that can run them FROM THE METHODOLOGY. The
generation is code-blind by construction: the generator only ever sees the
methodology document plus the data model / stage schemas, never the stage's
code or any existing tests — so calling this right after generating or
editing the code cannot anchor the tests on the implementation (that would
assert the code equals itself). Starts a background turn and returns
immediately; on completion the generated suite REPLACES the stage's tests
wholesale. Fails loudly if the stage's type carries no runnable tests, or it
has no output schema.""",
    ),
    "get_current_project": ToolSpec(
        name="get_current_project",
        description="""\
Return the id of the project this session is editing, or nothing if this
session was opened without one. Call this FIRST. If it returns an id, pass
that id as `project_id` to the other tools. If it returns nothing, no project
is bound: call list_projects and ask the user which one to work on — never
guess, and never pick one for them.""",
    ),
    "get_project_status": ToolSpec(
        name="get_project_status",
        description="""\
One project's full status snapshot: document present?, data-model state
(generating shows no schemas yet; then unapproved/approved), workflow stage
counts and review coverage, versions, runs. Poll this after generate_* to see
the result land.""",
    ),
    "get_run_status": ToolSpec(
        name="get_run_status",
        description="""\
The current manifest of one production run as a dict: its overall status
(running / ok / errors / halted), per-stage statuses, and run metadata. Poll
this after run_workflow to follow progress and see the outcome. An unknown or
expired run_id returns {ok: False, error} rather than a fabricated status.""",
    ),
    "sleep": ToolSpec(
        name="sleep",
        description="""\
Let time pass, then return: nothing else happens while you wait, and background
work carries on. This is how you wait for a run or a generation — the seconds a
job needs, then read its status once — rather than reading the same status over
and over as fast as you can call it. Returns the seconds it slept, which is
what you asked for clamped to the ceiling.""",
    ),
    "list_projects": ToolSpec(
        name="list_projects",
        description="""\
List every project in the workspace that has an authored workflow, as
{id, name} pairs. Pass the `id` to every other tool — `name` is a label the
author chose, it may be shared by two projects, and it identifies nothing. A
just-created project appears here only once its first stage has been added, so
a project missing from this list is one with no stages yet, not one that does
not exist.""",
    ),
    "profile_stage_output_data_range": ToolSpec(
        name="profile_stage_output_data_range",
        description="""\
Profile columns of one stage's stored output in a run: what the data actually
holds, for declaring a schema from the data rather than from prose. `columns`
is a LIST — ask for every column you are about to declare in one call.

Per column: `null_count`, `distinct_count` (the TRUE count of distinct non-null
values), `values` (commonest first, as text with their counts, cut to
`max_values`), `truncated`, and on a numerically typed column `value_range`
(min/max/mean/median). `truncated` means `values` is a prefix, not the
vocabulary — raise `max_values` before declaring an enum from it.

`row_count` is that stage's OWN output: far below the source under a filter or an
aggregate, and a sample either way off a sliced run. Naming a source stage in
run_workflow_test's `stage_ids` with `limit` null executes it over its whole
bound file — that is what gives an input column its complete vocabulary.

Reading the source file yourself answers a different question: the input stage
pins the declared dtypes (a zero-padded "002" declared `str` stays "002"; a plain
CSV read makes it 2), and a computed column is in no file at all.""",
    ),
    "read_data_model": ToolSpec(
        name="read_data_model",
        description="""\
The project's data model: every named schema as JSON (empty list if none
generated yet).""",
    ),
    "read_draft": ToolSpec(
        name="read_draft",
        description="""\
The draft's current stages plus `issues` — every cross-stage graph
problem (dangling input, duplicate id, cycle) it would fail on if saved
now ([] means save_version will succeed). Every stored stage is already
individually valid, so `issues` never covers a single stage's own shape.""",
    ),
    "read_review_guide": ToolSpec(
        name="read_review_guide",
        description="""\
The review guide stored on one saved version, or null when it has none. Read
before writing so you amend it rather than replace someone's work.""",
    ),
    "read_stage": ToolSpec(
        name="read_stage",
        description="""\
Return the JSON of one stage from the workflow. Read before editing.""",
    ),
    "remove_draft_stage": ToolSpec(
        name="remove_draft_stage",
        description="""\
Delete one stage from the draft by id. Removing a stage other stages
still input from leaves dangling edges — visible in `issues` until fixed.""",
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
    "report_compiler_warnings": ToolSpec(
        name="report_compiler_warnings",
        description="""\
Every problem with this workflow: undescribed stages, descriptions no examples
check, examples that do not pass, code the review panel cannot show, and
deliberate choices (cache off, row limit) a reviewer should be told about.
`errors` is the subset you can clear by editing the stage. This DOES run the
examples — a workflow whose examples disagree with its code is not
signed-off-able — but run_stage_tests is what tells you which case failed.""",
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
frozen test, and the fix is to the code (or to regenerate via
generate_stage_tests), never to bend the test to the code.""",
    ),
    "run_workflow": ToolSpec(
        name="run_workflow",
        description="""\
Start a REAL production run of the project's stored workflow and return
its `run_id` immediately — the run executes in the background. This is a run
of record: it writes a manifest under the project's runs/ dir and produces the
workflow's published artifacts. `version_id` pins a specific stored version,
published or not (omit for the newest stored one); a missing version is a
loud error, never a silent fallback. Poll get_run_status(project_id, run_id)
for live progress and the final status. On a pre-run failure (no stored
version, an unbound input) returns {ok: False, error} and starts no run.

`limits` caps how many rows a stage READS: {"<stage id>": N} gives that stage
its first N rows and leaves every other stage whole. It bounds the WORK, not
the spend of a stage that already ran — a cap on a stage downstream of a model
step does not stop that model step reading everything. Omit it for a full run.""",
    ),
    "run_workflow_test": ToolSpec(
        name="run_workflow_test",
        description="""\
Run a workflow test, so an author can watch the pipeline execute on real
data before publishing. It IS a real run — same `runs/` dir, manifest, and
trace/view routes as run_workflow's — over the same versions run_workflow
takes (any stored version, published or not; omit `version_id` for the newest
stored — so save_version first, there is no unsaved-edits mode). It differs
from run_workflow on exactly five axes:

1. SOURCE: the `limit` rows from `offset` of the workflow's bound source,
   injected (run_workflow reads the whole source through input_data). `limit`
   is the run's budget — every LLM stage pays per row, so state it; null is
   the whole source.
2. SCOPE: `stage_ids` names the stages to execute. A source stage named there
   EXECUTES instead of taking an injected frame, over the SAME `limit`/`offset`
   window — so naming a source with `limit` null is how you see an input
   column's complete vocabulary without paying for the stages below it. Every
   producer a named stage reads must be named too, or run over the injected
   slice, or that stage errors on its absent input. Omit `stage_ids` and every
   non-input stage runs.
3. EXECUTION: synchronous — this returns when the run is done (run_workflow
   returns a run_id immediately and executes on a background thread).
4. REVIEW QUEUE: a human_review_queue stage auto-approves every row in
   memory (run_workflow halts there and waits for a human).
5. STAGE CACHE: read-only — it may replay a workflow run's cached results
   but records none of its own, so it cannot affect a later run.

Marked `is_test_run` on the manifest, so it never counts as the project's
latest run. Returns the verdict {ok, run_id, version_id, stages_run, error}:
`stages_run` is what actually executed, and `ok` False on any stage error with
`error` naming what failed; poll get_run_status(project_id, run_id) for the
same live/final manifest run_workflow exposes, or
profile_stage_output_data_range for the values a stage produced. A project with
no stored version is a loud error.""",
    ),
    "set_draft_stage": ToolSpec(
        name="set_draft_stage",
        description="""\
Add or replace ONE stage in the draft (matched by the stage's `id`).
`stage_json` is the complete stage as a JSON object string. A MALFORMED
stage — invalid JSON, not an object, or failing the stage schema
(unknown type, missing required field, wrong shape, ...) — is REJECTED:
nothing is written, and you get the validation errors back to fix and
retry. A VALID stage whose `inputs` reference a stage id you have not
added yet IS stored — that's the workflow still being built, not a bad
stage — and shows up in the returned `issues`.""",
    ),
    "write_review_guide": ToolSpec(
        name="write_review_guide",
        description="""\
Store the walkthrough a human reads to understand what this version of the
workflow does. Replaces any guide already on that version, whole.

Written in TEST_RUN_REVIEW — after this version's smoke run, not off the back
of save_version.""",
    ),
}

# `save_version` is one NAME for two operations, because the surfaces author into
# different places: the MCP server edits the working copy, the editing agent
# builds a draft. Unifying them means retiring the working copy — see issue #357.
SAVE_VERSION_FROM_WORKING_COPY = ToolSpec(
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
)

SAVE_VERSION_FROM_DRAFT = ToolSpec(
    name="save_version",
    description="""\
Freeze the draft into a new immutable version — your proposal for a
human to review. Validates the whole workflow first: an invalid draft is
refused with the full issue list and nothing is written. The version is
born UNPUBLISHED; only a human can publish it, and publishing records that
they have read it. `message` says what changed and why, for the reviewer.
Save once per finished proposal, not per edit.""",
)

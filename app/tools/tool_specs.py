"""Every tool description both authoring surfaces read, keyed by tool name.
The glassbox MCP server and the editing agent expose overlapping tools; holding the
prose once is what stops the two drifting. `save_version` is the one name meaning two
different operations — see SAVE_VERSION_* below."""
from __future__ import annotations

from app.core.agent.tool_spec import ToolSpec
from app.models.observation import DEFAULT_MAX_DISTINCT_VALUES

# How to use list_distinct_values when declaring a column's schema — shared by the
# editing agent's system prompt and the glassbox instructions, held once so the two
# surfaces cannot drift on when a vocabulary deserves freezing. Both prompts embed
# it verbatim; tests assert the embedding.
OBSERVED_ENUM_GUIDANCE = """\
Declaring enums from observed data: author a categorical-looking column (a
status, a category, a reason code) as a bare type, run a workflow test, then LOOK
at what it produced — list_distinct_values(project_id, run_id, stage_id, column)
reports one stage output's real vocabulary, and edit_stage tightens the schema
afterwards. run_workflow_test(project_id, use_working_copy=True) runs the stages
you are editing right now, so nothing has to be saved as a version first. The
document's prose is a guess; the run is evidence.
Read the profile before trusting it: distinct_count above len(values) means the
list was TRUNCATED — re-read with a larger max_values, since a vocabulary can be
thousands long and still closed — and row_count is THAT STAGE's output, which
below a filter or an aggregate is a fraction of the source. A set frozen off a
short tail is still a guess.
Then DECIDE, per column, whether to freeze the observed set as its `enum`. The
declaration is the maintenance surface a NON-ENGINEER data owner lives with, and
a value outside a declared enum FAILS the stage. Freeze the sets whose GROWTH
should stop and be reviewed; leave open the ones that legitimately grow.
- Freeze: `permit_status`, 3 values across 12,000 rows — filed | granted |
  denied — the statuses the methodology reasons about. A fourth means the source
  changed underneath the workflow; declare enum: ["filed", "granted", "denied"]
  so that surfaces instead of flowing through unexamined.
- Leave open: `city`, 38 values. Small but not closed — next month's export may
  legitimately name a new one, and stopping the run for that would be noise.
  Leave it a bare `str`.
Two stages observation cannot settle for you: an `llm_transform` column whose
enum you already declared compiles into that stage's reply model, so the run
returns a subset of your own declaration and corroborates nothing; and a
`human_review_queue` decision column auto-approves every row in a TEST run, so
its observed values are an artifact of the test, not the data.
An enum never replaces guard code: a rule a declaration cannot state (a
cross-column consistency rule, normalization before comparison) still belongs
in the stage's authored code."""

TOOL_SPECS: dict[str, ToolSpec] = {
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
Summarize a project's workflow: each stage's id, type, name, upstream input
ids, and review state. Read this before editing so you know the current
shape. Does not return full stage specs — use read_stage for one.""",
    ),
    "edit_stage": ToolSpec(
        name="edit_stage",
        description="""\
Change specific fields of one stage. `changes_json` is a JSON object of
ONLY the fields to change (a JSON Merge Patch): {"limit": 100} sets limit;
{"llm": {"model": "claude-opus-5"}} changes only llm.model and leaves the rest of the
llm block intact; {"name": null} deletes a field. Fields you do not mention
are preserved exactly. Validated first; if invalid, nothing is written and
the issues are returned. A successful edit drops the node to 'edited_stale'
for a human to re-approve — you cannot approve it yourself. You cannot
change a stage's id this way.""",
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
Return the id of the project this session is editing. Call this FIRST and
pass its value as `project_id` to the other tools.""",
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
    "list_distinct_values": ToolSpec(
        name="list_distinct_values",
        description=f"""\
The observed distinct values of ONE column of ONE stage's output in ONE run —
read from what that run actually wrote, never from the methodology's prose.
Any stage that produced an output is readable, not just the inputs, so run a
workflow test first and observe the stage you care about.

`run_id` is REQUIRED: there is no latest-run default, because which run you
read is part of what the answer means. The reply names the run and stage back
to you, plus row_count (that OUTPUT's size — a stage below a filter or an
aggregate sees far fewer rows than the source), null_count, distinct_count
(the TRUE number of distinct values) and `values`, sorted and truncated to
`max_values` (default {DEFAULT_MAX_DISTINCT_VALUES}). `values` is the COMPLETE
vocabulary ONLY when distinct_count == len(values); when distinct_count is
larger you are looking at a truncated prefix, so re-read with a bigger
max_values before freezing anything — a large vocabulary can still be a closed
one. Consult it before deciding whether a column's schema should freeze its
vocabulary as an `enum`; your instructions say how to decide. Fails loudly —
never inventing a value — for an unknown project, an unknown run, a stage that
wrote no output in that run, or a column that output does not hold; each names
the real alternatives.""",
    ),
    "list_projects": ToolSpec(
        name="list_projects",
        description="""\
List the names of every project in the workspace that has an authored
workflow. A just-created project appears here only once its first stage has
been added, so a name missing from this list is a project with no stages
yet, not a project that does not exist.""",
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
`blocking` is the subset you can clear by editing the stage. This DOES run the
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
Start a REAL production run of the project's published workflow and return
its `run_id` immediately — the run executes in the background. This is a run
of record: it writes a manifest under the project's runs/ dir and produces the
workflow's published artifacts. `version_id` pins a specific published version
(omit for the newest published one); an unpublished or missing version is a
loud error, never a silent fallback. Poll get_run_status(project_id, run_id)
for live progress and the final status. On a pre-run failure (nothing
published, an unbound input) returns {ok: False, error} and starts no run.""",
    ),
    "run_workflow_test": ToolSpec(
        name="run_workflow_test",
        description="""\
Run a workflow test, so an author can watch the pipeline execute on real
data before publishing. It IS a real run — same `runs/` dir, manifest, and
trace/view routes as run_workflow's — and differs from run_workflow on
exactly five axes:

1. WORKFLOW: any stored version, published or not (run_workflow pins a
   published one) — omit `version_id` for the newest stored — OR, with
   `use_working_copy: true`, the stages you are editing right now, saved as no
   version at all. That is what lets you observe a stage's real output before
   deciding its schema. Naming BOTH a version and the working copy is a loud
   error; a working-copy run reports `version_id` null, because there is none.
2. SOURCE: the `limit` rows from `offset` of the workflow's bound source,
   injected (run_workflow reads the whole source through input_data). The
   slice is still written out as the input stage's own output, so every stage
   of the graph — inputs included — is readable back with
   list_distinct_values.
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
run_workflow exposes. Asking for a stored version when the project has none is
a loud error — `use_working_copy: true` is the way to test before any exists.""",
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
workflow does. Replaces any guide already on that version, whole.""",
    ),
}

# `save_version` is one NAME for two operations, because the surfaces author into
# different places: the glassbox server edits the working copy, the editing agent
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
born UNPUBLISHED; only a human can publish it (runs execute published
versions only). `message` says what changed and why, for the reviewer.
Save once per finished proposal, not per edit.""",
)

"""Every tool the authoring surfaces offer, keyed by tool name.
One record per tool: an AgentTool's body, its label and the prose the model reads sit
together, so binding it is a single lookup and no half of it can go missing.
"""
from __future__ import annotations

from app.core.agent.bound_tool import BoundToolSpec, bind_by_signature
from app.tools import shared
from app.tools.shared import MAX_OUTPUT_ROWS, MAX_RUNS_LISTED, MAX_SLEEP_SECONDS
from app.tools.types import AgentTool, ToolParameterProse

# An id is a stamp, not a label: the prose has to send the model to look one up.
PROJECT_ID = (
    "The project's id, from list_projects or the address get_current_url returns — an "
    "opaque stamp like "
    "`20260818T090501.047918`, never a name you can guess."
)

# read_stage_output_rows builds links, so its reader's address is the CALLER's to
# supply — never something the model is asked for.
_CALLER_SUPPLIED = frozenset({"base_url"})


def bind(*names: str) -> list[BoundToolSpec]:
    """The named tools as BoundToolSpecs — an agent config lists names, not bodies."""
    return [
        bind_by_signature(
            name=name,
            description=AGENT_TOOLS[name].description,
            fn=AGENT_TOOLS[name].fn,
            label=AGENT_TOOLS[name].label,
            parameters=AGENT_TOOLS[name].parameters,
            skip=_CALLER_SUPPLIED,
        )
        for name in names
    ]


def read_tool_description(name: str) -> str:
    """What the model is told a tool does, wherever the body behind it lives."""
    agent_tool = AGENT_TOOLS.get(name)
    if agent_tool is not None:
        return agent_tool.description
    return SURFACE_TOOL_DESCRIPTIONS[name]


def read_parameter_prose(name: str) -> ToolParameterProse:
    """For a surface that WRAPS one of these instead of binding it."""
    return AGENT_TOOLS[name].parameters


def find_tool_names() -> set[str]:
    """Every name that is a tool on some surface."""
    return set(AGENT_TOOLS) | set(SURFACE_TOOL_DESCRIPTIONS)


# ── the tools whose body lives here ──────────────────────────────────────────

AGENT_TOOLS: dict[str, AgentTool] = {
    "approve_code_execution": AgentTool(
        fn=shared.approve_code_execution,
        label="Turning on code execution",
        parameters={
            "project_id": PROJECT_ID,
            "reason": "What the Python step will do, and why no declared stage fits — in the "
                "words you put to the owner. Stored, so whoever turns this off later can "
                "see what was agreed to.",
        },
        description="""Turn on unsandboxed Python (`python_frame_function`) for this project.

DO NOT CALL THIS TO ASK. It records an answer the owner has ALREADY GIVEN,
in this conversation, in reply to the warning. Calling it before they have
answered turns on code execution on their machine without their consent.

The order is: try the declared stages first — `explode` then a
`starlark_row_function` covers most of what this type used to be reached
for, and `dedupe`, `sort_rank`, `aggregate`, `enrich`, `expand` and `union`
cover the reshapes. Only if none fits, tell the owner plainly what the step
will do, that Carbon Paper is not built for arbitrary code execution, that
the step runs on their machine with their permissions and can read files
and reach the network, and that a trace stops at it. Then ask. Then wait.

If they say yes, call this. If they say no, or say nothing, do not call it
— say what you cannot build and stop.

It stays on for the WHOLE project until a person turns it off, so tell them
that too. Approving twice is not an error and does not extend anything.""",
    ),
    "read_terms": AgentTool(
        fn=shared.read_terms,
        label="Reading the project's words",
        parameters={"project_id": PROJECT_ID},
        description="""\
The project's agreed vocabulary: its NOUNS (the things its data is about,
each with the columns it has if it has any) and its VERBS (the acts the
methodology performs), plus the other spellings its owner writes each one
as. These words are handed to every agent that writes prose about this
project — stage descriptions, generated examples, the review guide — and
the human reads them on the project's Terms page, so what you read here is
what your writing has to match. A word not in this list is a word to agree
with the user, never one to coin. An empty result means the words have not
been agreed yet, not that the project has none.""",
    ),
    "write_terms": AgentTool(
        fn=shared.write_terms,
        label="Storing the project's words",
        parameters={
            "project_id": PROJECT_ID,
            "terms": "The WHOLE vocabulary — `nouns` and `verbs` both, every time. What you send "
                "replaces what is stored, so read_terms first and send that back with your "
                "additions.",
        },
        description="""\
Store the words this project is written in — the WHOLE vocabulary, both
halves, every time. What you send REPLACES what is stored: a noun or verb
you leave out is one the project stops using, so read_terms first and send
that back with your additions rather than sending only what is new.

A noun is a named schema. One that is nothing but a word — a thing the
methodology talks about with no table behind it — carries a `name` and a
`title` and no columns and no `kind`; that is the ordinary case, not a
half-finished one. Add columns only where you know the fields. A verb
carries its name and what it means. Either may list `also_written`: the
other spellings the owner uses for that same thing.

REFUSED WHOLE, with nothing written, where one word carries two meanings —
a noun and a verb of the same name, or two words sharing a spelling. The
refusal names the repeated word. It is not a formality: a stage
description written in an ambiguous word leaves the reader unable to tell
which thing it meant.

What is stored reaches every agent that writes prose about this project and
is shown to the human on the project's Terms page. Agree the words with the
user before you store them — never invent one to fill the list out.""",
    ),
    "get_project_status": AgentTool(
        fn=shared.get_project_status,
        label="Checking the project",
        parameters={"project_id": PROJECT_ID},
        description="""\
One project's full status snapshot: document present?, data-model state
(generating shows no schemas yet; then unapproved/approved), workflow stage
counts and review coverage, versions, runs. Poll this after generate_stage_tests
to see the result land.""",
    ),
    "list_projects": AgentTool(
        fn=shared.list_projects,
        label="Listing projects",
        parameters={},
        description="""\
List every project in the workspace that has an authored workflow, as
{id, name} pairs. Pass the `id` to every other tool — `name` is a label the
author chose, it may be shared by two projects, and it identifies nothing. A
just-created project appears here only once its first stage has been added, so
a project missing from this list is one with no stages yet, not one that does
not exist.""",
    ),
    "read_stage": AgentTool(
        fn=shared.read_stage,
        label="Reading a stage",
        parameters={
            "project_id": PROJECT_ID,
            "stage_id": "The stage's id, as read_workflow_summary shows it.",
        },
        description="""\
Return the JSON of one stage from the workflow. Read before editing.""",
    ),
    "delete_stage": AgentTool(
        fn=shared.delete_stage,
        label="Removing a stage",
        parameters={
            "project_id": PROJECT_ID,
            "stage_id": "The stage to delete. Refused if another stage still lists it in its inputs.",
        },
        description="""\
Delete one stage from the workflow — the undo for a stage you added. The
workflow WITHOUT the stage is validated first: if another stage still lists it
in `inputs`, the removal is refused, nothing is deleted, and the issues are
returned (remove or repoint the downstream stage first). Removing the last
remaining stage is allowed.""",
    ),
    "read_review_guide": AgentTool(
        fn=shared.read_review_guide,
        label="Reading the review guide",
        parameters={
            "project_id": PROJECT_ID,
            "version_id": "The version whose guide to read.",
        },
        description="""\
The review guide stored on one saved version, or null when it has none. Read
before writing so you amend it rather than replace someone's work.""",
    ),
    "write_review_guide": AgentTool(
        fn=shared.write_review_guide,
        label="Writing the review guide",
        parameters={
            "project_id": PROJECT_ID,
            "version_id": "The version this guide describes. The guide is validated against THAT "
                "version's stages.",
            "guide": "The complete guide: `steps`, each with `title`, `prose` and `stage_ids`, "
                "plus `unnarrated`. Sent whole every time — it replaces any earlier guide.",
        },
        description="""\
Store the walkthrough a human reads to understand what this version of the
workflow does. Replaces any guide already on that version, whole.

Written in TEST_RUN_REVIEW — after this version's smoke run, not off the back
of save_version.""",
    ),
    "run_stage_tests": AgentTool(
        fn=shared.run_stage_tests,
        label="Running the stage's tests",
        parameters={
            "project_id": PROJECT_ID,
            "stage_id": "One stage to scope the run to. Omit to run every stage with tests.",
        },
        description="""\
Run a stage's authored tests against its CURRENT code and report the
result. Omit `stage_id` to run every stage that has tests, or pass one to
scope the run to that stage. Use this after regenerating code
with edit_stages to see which tests the new code fails — the report carries a
summary plus, per test, its status and any cell diffs, and lists
`untested_stages` (testable stages with no tests, a coverage gap).
This does NOT edit tests: a failing test means the code disagrees with the
frozen test, and the fix is to the code (or to regenerate via
generate_stage_tests), never to bend the test to the code.""",
    ),
    "report_compiler_warnings": AgentTool(
        fn=shared.report_compiler_warnings,
        label="Reading the workflow's warnings",
        parameters={"project_id": PROJECT_ID},
        description="""\
Every problem with this workflow: undescribed stages, descriptions no examples
check, examples that do not pass, code the review panel cannot show, and
caching turned off on a model or review stage. None of them
refuses anything — a human decides what to fix and what to leave standing. This
DOES run the examples, but run_stage_tests is what tells you which case failed.""",
    ),
    "generate_stage_tests": AgentTool(
        fn=shared.generate_stage_tests,
        label="Generating the stage's tests",
        parameters={
            "project_id": PROJECT_ID,
            "stage_id": "The stage to generate tests for.",
        },
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
    "run_workflow": AgentTool(
        fn=shared.run_workflow,
        label="Running the workflow",
        parameters={
            "project_id": PROJECT_ID,
            "version_id": "Omit for the project's newest stored version.",
            "limits": 'Caps how many rows a stage READS: {"<stage id>": N}.',
            "files": 'The stored file each input stage reads for THIS run: '
                '{"<stage id>": "<file_id from list_files>"}.',
            "bust_cache": "Recompute every stage instead of replaying what a previous run "
                "cached. Costs whatever the cached stages cost the first time — on an "
                "`llm_transform` that is real money. Use it to check a result is "
                "reproducible, not as a matter of course.",
        },
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
step does not stop that model step reading everything. Omit it for a full run.

`files` binds a stored file to an input step for this run only:
{"<input stage id>": "<file_id from list_files>"}. A file_id the project does not
hold is a loud error naming itself, not a silent unbound input. Omit it where
the workflow already names the file it reads.""",
    ),
    "run_workflow_test": AgentTool(
        fn=shared.run_workflow_test,
        label="Testing the workflow on real rows",
        parameters={
            "project_id": PROJECT_ID,
            "limit": "How many rows of the bound source to run on — the run's budget, since every "
                "LLM stage pays per row. null runs the whole source.",
            "version_id": "Omit for the project's newest stored version.",
            "stage_ids": "Which stages to execute. Omit to run every stage that is not an input.",
            "offset": "The source row the window starts at. 0 is the first.",
        },
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
    "list_runs": AgentTool(
        fn=shared.list_runs,
        label="Listing the project's runs",
        parameters={
            "project_id": PROJECT_ID,
            "limit": f"How many of the newest runs to name. Clamped to {MAX_RUNS_LISTED}.",
        },
        description="""\
This project's production runs, newest first: each one's id, status, when it
started, the version it pinned, and whether it was a test run. The run id is what
get_run_status and read_stage_output_rows take, and the only way to name a run
you did not start yourself. `run_count` is every run the project has, so a
listing cut to `limit` reads as the window it is.""",
    ),
    "get_run_status": AgentTool(
        fn=shared.get_run_status,
        label="Checking the run",
        parameters={
            "project_id": PROJECT_ID,
            "run_id": "The run id run_workflow returned.",
        },
        description="""\
The current manifest of one production run as a dict: its overall status
(running / ok / errors / halted), per-stage statuses, and run metadata. Poll
this after run_workflow to follow progress and see the outcome. An unknown or
expired run_id returns {ok: False, error} rather than a fabricated status.""",
    ),
    "sleep": AgentTool(
        fn=shared.sleep,
        label="Waiting",
        parameters={
            "seconds": f"How long to sleep. Clamped to {MAX_SLEEP_SECONDS} — sleep again to wait longer.",
        },
        description="""\
Let a few seconds pass, then return: nothing else happens while you wait, and
background work carries on. This is how you wait for a run or a generation —
sleep, read its status, sleep again while it is still going — rather than
reading the same status over and over as fast as you can call it. Sleeps are
deliberately short: a reader watching this conversation sees each call, so a
short one reads as work in progress where a long one reads as a hang. Returns
the seconds it slept, which is your ask clamped to the ceiling.""",
    ),
    "read_workflow_summary": AgentTool(
        fn=shared.read_workflow_summary,
        label="Reading the workflow",
        parameters={"project_id": PROJECT_ID},
        description="""\
Summarize a project's workflow: each stage's id, type, description, upstream
input ids, and review state. Read this before editing so you know the current
shape. Does not return full stage specs — use read_stage for one.""",
    ),
    "read_stage_output_rows": AgentTool(
        fn=shared.read_stage_output_rows,
        label="Reading the stage's rows",
        parameters={
            "project_id": PROJECT_ID,
            "run_id": "The run whose stored output you want to read.",
            "stage_id": "The stage whose output rows you want.",
            "limit": f"How many rows to read, from `offset`. Clamped to {MAX_OUTPUT_ROWS}, which "
                f"is also the default.",
            "offset": "The row ordinal to start at. 0 is the first row.",
        },
        description="""\
The ROWS one stage of a run produced, as stored: a window of at most 50, from
`offset`, each carrying its `ordinal`, its cell `values`, and the `lineage_url`
of that row's lineage page — a whole link, to hand on as it stands. `row_count`
is the stage's entire output and `limit` is the window actually applied, which
is smaller than you asked for when you asked for more than the cap.

This is the tool for a QUESTION ABOUT A ROW — what a stage did to a given
filing, which rows carry a blank, what a model actually answered. A question
about what a COLUMN holds across the whole output — its vocabulary, its range,
how many nulls — is a profile, and paging through rows answers it slowly and
partially.

A row's ordinal is recorded nowhere else, so a lineage link not read from here
is a guess. A stage that did not finish is refused rather than read: an errored
stage still wrote a frame, and the columns it never reached are nulls, not
results.""",
    ),
    "profile_stage_output_data_range": AgentTool(
        fn=shared.profile_stage_output_data_range,
        label="Reading what the stage's columns hold",
        parameters={
            "project_id": PROJECT_ID,
            "run_id": "The run whose stored output you want to profile.",
            "stage_id": "The stage whose output columns you want.",
            "columns": "The columns to profile — every one you are about to declare.",
            "max_values": "How many distinct values to show per column, commonest first. `truncated` "
                "says whether there were more.",
        },
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
    "move_file_to_project": AgentTool(
        fn=shared.move_file_to_project,
        label="Putting the file in the project",
        parameters={
            "project_id": PROJECT_ID,
            "file_id": "The stored file's file_id, as list_files reported it.",
        },
        description="""\
Put a file that is in no project into one. Moves no bytes.""",
    ),
    "profile_file": AgentTool(
        fn=shared.profile_file,
        label="Reading what the file holds",
        parameters={
            "project_id": PROJECT_ID,
            "file_id": "The stored file's file_id, as list_files reported it.",
            "columns": "Which columns to profile. Omit for every column in the file.",
            "max_values": "How many distinct values to show per column, commonest first. "
                "`truncated` says whether there were more.",
            "sheet_name": "xlsx only: the sheet, by name or 0-based position.",
            "header_row": "xlsx only: the 0-based row the header sits on.",
            "first_column": "xlsx only: the 0-based column the table starts at.",
        },
        description="""\
What a stored file holds, by the `file_id` list_files gave it.

Returns `row_count` and, per column, `null_count`, `distinct_count` (the TRUE
count of distinct non-null values), `values` (commonest first with their counts,
cut to `max_values`), `truncated`, and `value_range` (min/max/mean/median) where
every value is a number. Omit `columns` for every column in the file.

This makes no judgement about types — that decision is yours. csv and xlsx are
read as text and json with inference off, so `values` are the characters as
stored and nothing is coerced. parquet and geojson carry real types, which are
respected as they are.

`truncated` means `values` is a prefix, not the whole vocabulary — raise
`max_values` before declaring an enum from it.

For an xlsx, `sheet_name`, `header_row` and `first_column` say which table to
read — the same three the input_data connector takes, so what you profile here
is what the stage will read. They default to the first sheet, first row as the
header, first column. survey_workbook is how you find out what to pass.""",
    ),
    "survey_workbook": AgentTool(
        fn=shared.survey_workbook,
        label="Looking over the workbook's sheets",
        parameters={
            "project_id": PROJECT_ID,
            "file_id": "The stored xlsx's file_id, as list_files reported it.",
            "from_row": "The 0-based sheet row the window starts at. Raise it to look past a "
                     "preamble longer than the window.",
        },
        description="""\
The sheets in a stored xlsx. Per sheet: its `name`, its `row_count` and
`column_count`, and `cells` — a 5-row by 8-column window of the sheet exactly as
it sits, no header picked and nothing skipped.

`cells` is a grid, so POSITION IS THE INDEX: `cells[2][1]` is the third row,
second column. Those two indices are the `header_row` and `first_column` you
then pass to profile_file. Read them off the values, which is what tells the
three cases apart — one long string alone on a row is a title, a row of short
field-like names is the header, and the row under it is data.

    cells[0] = ["LOBBYING DISCLOSURE — Q1 2026", null, null]
    cells[1] = [null, null, null]
    cells[2] = [null, "registrant", "filings"]     -> header_row=2, first_column=1

`first_row` says which sheet row `cells[0]` is, and `from_row` moves the window
down. A sheet whose whole window is prose has its header further down: survey it
again from where the prose ran out. The window does not hunt for the table —
nothing here guesses which row is the header, because a wrong guess is a schema
declared against the wrong columns.

`row_count` is the extent the sheet declares, which counts a trailing styled but
empty row, so it is an upper bound rather than the count a read gives.

Refused for every other format: they hold one table and no sheets.""",
    ),
}

# ── the tools whose body the offering surface writes ─────────────────────────
# Each needs something only its own surface has — the session's project, the address
# its reader clicks, which surface authored a project — so there is no body to hold
# here, only the description. Holding that once is what stops the surfaces drifting.

SURFACE_TOOL_DESCRIPTIONS: dict[str, str] = {
    "add_stage": """\
Create NEW stages in the workflow. `stages` is a LIST — submit every stage
you are ready to author in ONE call; a list of one is the single-stage case.
Each is a FULL stage, as the anatomy describes one. Its `id` is new, unique,
and the stage's ONLY name — every surface shows it, so name the step well;
use edit_stages to change an existing one. `report` is the one type needing
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
    "create_project": """\
Create a NEW project from a methodology document (prose describing how the
investigation finds, verifies, and surfaces its claims). Writes the document
as the project's source of record and returns the project_id every other tool
takes.

That id is MINTED, and it is not the name: `name` is a label, two projects may
carry the same one, and a repeated name is never refused. So a project is only
ever addressed by the id this call returns — a name you were given by a human,
or read off a page, identifies nothing.

An empty document is refused and no project is written. Next: agree the
project's terms — the words its methodology already uses — with the user, and
store them with write_terms.""",
    "edit_stages": """\
Change specific fields of a stage. `changes_json` is a JSON object of
ONLY the fields to change (a JSON Merge Patch): {"cache": true} turns caching on;
{"llm": {"model": "claude-opus-5"}} changes only llm.model and leaves the rest of the
llm block intact; a null value deletes a field. Fields you do not mention
are preserved exactly. Validated first; if invalid, nothing is written and
the issues are returned. You cannot change a stage's id this way.

Edit several stages at once by sending several entries — they are validated and
written as one workflow, which is what edits that only make sense together need.""",
    "get_current_url": """\
The page the reader has open right now, which moves as they browse. Call it when
they say "this" or "here", and read the ids out of the address instead of asking
for one. Nothing if the chat surface did not report a page.""",
    "save_version": """\
Freeze the project's CURRENT workflow into an immutable version — the snapshot
a run or a workflow test executes.

`message` NAMES the version for a reader scanning a list: one line, 150
characters at the outside, and a longer one is refused rather than trimmed.
Say what changed, not why or how — "Carry every spelling of the firm name"
rather than a paragraph of release notes.

`parent_version` is the version YOU started this edit from. Supply it only when you
actually loaded that version; it is recorded verbatim as this snapshot's ancestor,
and an id naming no version of this project is refused. Omitting it is normal and
records no ancestor — nothing is inferred from what else the project has stored.

The working copy is strict-loaded first, so an invalid workflow comes back as
{ok: False, issues} and no version is written.""",
    "list_files": """\
The files a project holds, each with the `file_id` run_workflow's `files` binds.
`project_id` null lists the files that are in no project yet.

Also returns `file_upload_url`: POST a file there as multipart form data to add
one. Nothing in this conversation moves bytes.""",
}

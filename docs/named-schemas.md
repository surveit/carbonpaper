# Named schemas + the eval data model

Two related pieces of `app/models/`: the named-schema data model (author tables
first, wire the workflow over them) and the eval model (grade a workflow against
an eval-dataset table). Both exist on master as **validated Pydantic models**; neither is
yet consumed by the runtime, and no committed example exercises them end-to-end.
This doc describes the models as they are.

## Named schemas — the data model as a first-class artifact

**Named schemas** let the data model be authored as its own artifact, with the
workflow wired over it, rather than read off the stages of a workflow you must
author first.

Why it matters (the forcing example from the LobbyMap work): you cannot write a
benchmark-scoring stage until `query`, `data_source`, and `benchmark` exist as
tables — and nothing in the pipeline *produces* those; they're reference data. A
workflow-first tool has nowhere to put them.

The contract, in `app/models/named_schemas.py`:

- A **`NamedSchema`** is a `TableSchema` (columns + primary key) plus:
  - `name` — snake_case identity.
  - `also_written` — the methodology's other spellings for the same thing (the
    document's *firm*, the data's *registrant*). `Verb` carries the same field,
    and `Terms` checks the two halves' names and spellings together.
  - `kind` (`SchemaKind`, optional) — where the table sits in the pipeline. A
    kind is a claim about where the rows come from, so a name that is vocabulary
    alone (see Terms below) carries none:
    - `reference` — dimension / lookup / benchmark data we must SOURCE, not
      compute (the "missing intermediate databases" of the forcing example).
    - `input` — raw data fetched into the pipeline.
    - `computed` — produced by a workflow stage.
    - `ground_truth` — external truth used only by eval.
- A **`NamedColumn`** may carry `references: <schema>` or `<schema>.<column>` —
  an explicit foreign key, making the data model a real graph rather than
  PK-name-collision guessing. `validate_references_resolve` validates the graph.
- A **`SchemaLibrary`** is the set of a project's named schemas;
  `parse_schema_library` / `validate_schema_library` are the entry points.

## Terms — the nouns and the verbs

`app/models/terms.py` composes the two halves of a project's vocabulary: `Terms`
is a `SchemaLibrary` (the nouns — a noun that is only a word carries no `kind`
and no columns) plus `list[Verb]` (`name`, `definition`, `also_written`).
Constructing it refuses a word carried twice across either half, since a word
meaning two things is what the artifact exists to catch.

Storage is `app/services/terms.py`, the sole reader and writer of both halves:
one `StoredTerms` document per project in the `terms` collection of the document
store, keyed `<project_id>/terms`. The halves are stored apart and `load_terms`
composes them, which is where a word carrying two meanings raises; a project that
stored nothing loads empty Terms. `count_nouns` is the tolerant count the project
card and the status snapshot use, so one unreadable noun does not blank a listing.

Projects authored before that collection existed hold their nouns as one file per
schema under `<project>/schemas/`. Those files are still READ, by `load_terms`
alone and only where the store holds no document for the project; nothing writes
them, so the first `write_terms` moves that project into the store for good.

A project export (`WorkflowFile`) carries `data_model` and `verbs` as separate
fields, so a bundle written before verbs existed still imports.

`render_terms` (also `app/models/terms.py`) is the one block every agent writing about
a project is handed them in: the MCP `read_terms`/`write_terms` tools store them, the
editing agent gets them appended to its system prompt per session
(`AgentConfig.render_session_prompt`), and both one-shot generators carry them in their
task. A project with no words renders nothing at all. `/project/<id>/glossary` is where a
human reads them.

**What is NOT here (yet):** no `schemas/` directory ships in any project;
workflow stages do not structurally import named schemas (an import mechanism
was built and deliberately reverted as premature — stages reference schemas
loosely, by intent). If you re-introduce coupling, make it loose first.

## The eval data model

Hard rule: **eval must not leak into the generation data model.** Generation has
no knowledge that eval exists; eval depends on generation, one-directional.

The contract, in `app/models/eval.py` (see its module docstring — it's the
authoritative description):

- An **`EvalConfig`** is the authored spec. Its core is ONE row-aligned
  eval-dataset table: each row's columns are `override_stage`'s output
  (injected as that stage's whole output), plus one expected-output column
  per check (`expected_outputs`, each an `ExpectedOutput.output_column`)
  compared against `target_stage`'s output on the same row. Because it is a
  single table, injected input and expected output are 1:1 **by
  construction**.
- That 1:1 alignment is only well-defined when every stage on the
  override→target path preserves grain (no fan-out/fan-in).
  `resolve_eval_run_settings` (`app/evals/run_settings.py`) walks the path and
  checks each stage's `is_grain_and_order_preserving` (fixed per stage type in
  `app/models/stages/stage_base.py`; the `python_row_function` type exists
  precisely so the runtime *enforces* the 1:1 guarantee rather than trusting
  it); a non-preserving stage makes the eval non-scorable and the settings say
  why.
- **`StageOutputOverride`** injects a whole table as some stage's output,
  cutting that stage and everything upstream out of the run —
  `reference_overrides` use this to supply extra data an eval-dataset row
  needs.
- An **`EvalRun`** records the result at a specific workflow version: resolved
  settings, metrics, per-row results (no overall pass/fail — see the model
  docstring).
- Escape hatch: a `code` scorer replaces the declarative per-column comparison
  when it can't express the grading.

**What is NOT here (yet):** no runner integration (nothing executes an
`EvalConfig`), and no committed eval configs. Storage is specified in
[models-and-storage.md](models-and-storage.md)
(`<object_type>/<object_id>.data`).

## Authoring tips / gotchas

- Don't enumerate data you don't have (e.g. a full query list) — model the
  table, leave rows to be sourced; note the gap in `notes:`.
- A `-2..+2` score plus "not applicable" / "not scored" states does NOT fit one
  numeric column — model a `status` enum + a nullable `score` (absence ≠ zero).
  This is a recurring trap in scoring pipelines.
- **Beware `extra="ignore"`**: the shared model base (`app/models/schema.py`
  `_Base`) silently drops unknown keys, so a mistyped field name (or a
  constraint added under the wrong key) disappears without an error. Check your
  spelling against the model; don't trust silence.

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
  - `kind` (`SchemaKind`, optional) — where the table sits in the pipeline. A
    kind is a claim about where the rows come from:
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

## Terms — the row types, the tables and the verbs

`app/models/terms.py` composes the three parts of a project's vocabulary: `Terms`
is `list[RowType]` (`app/models/row_types.py` — `id`, `title`, `definition`: the
methodology's word for what ONE ROW is) plus a `SchemaLibrary` (the tables) plus
`list[Verb]` (`name`, `definition`). Nothing binds a table to the row type its rows
are: the two halves sit side by side, and only the migration's join treats a shared
name as a link. A schema NAME addresses a table rather than saying a word, so it is
not vocabulary. Constructing `Terms` refuses a word carried twice across the row
types and the verbs.

A STAGE does bind to one. `row_type_id` on `AuthoredStageFields`
(`app/models/stages/stage_base.py`) names which row type ONE of that stage's output
rows is, and only the seven types that answer for their own rows may carry it —
`declares_its_own_row_type`. Every other stage's rows are still its input's kind of
thing, so it names none and `resolve_row_type_ids` (`app/models/workflow.py`) reads the
word down the graph to it.

One field, three states. Absent means nobody has answered, which is every stage written
before the field existed; an answering type that leaves it absent raises the
`unnamed_rows` compiler warning rather than being refused. A row type's id means these
rows are that kind of thing. `NO_KIND_ROW_TYPE_ID` — the reserved id `no_kind`, which
`RowType` refuses so a project can never coin it — means they are not a kind of thing at
all: a `report` emits files rather than rows, and an `aggregate` with no `group_by` emits
one figure ABOUT the whole input population. Absence cannot carry that answer, because
both stage records dump with `exclude_none=True` and would strip an explicit null.
Alembic `0022` backfills the two mechanical cases across every stored stage spec.

Storage is `app/services/terms.py`, the sole reader and writer of all three: one
`StoredTerms` document per project in the `terms` collection of the document
store, keyed `<project_id>/terms`. The parts are stored apart and `load_terms`
composes them, which is where a word carrying two meanings raises; a project that
stored nothing loads empty Terms. `count_schemas` is the tolerant count the project
card and the status snapshot use, so one unreadable table does not blank a listing,
and `has_terms` is the same tolerant read behind the overview's "Agree the project's
terms" — true of a project that agreed only words, with no table yet.

Projects authored before that collection existed hold their nouns as one file per
schema under `<project>/schemas/`. Those files are still READ, by `load_terms`
alone and only where the store holds no document for the project; nothing writes
them, so the first `write_terms` moves that project into the store for good. Each
file mints both a row type and the table holding its rows —
`split_pre_row_type_nouns`, which alembic `0021` also runs over the stored
documents it rewrites from v1, dropping the table of a stored noun that said
nothing about one.

A project export (`WorkflowFile`) carries `data_model`, `row_types` and `verbs` as
separate fields, so a bundle written before either of the last two existed still
imports. It is also the one place holding a project's stages and its row types at
once, which is where a stage naming a word the row types do not hold is refused —
`find_undeclared_row_type_issues`, naming the stage and the unknown word. A stage that
leaves `row_type_id` unset names no word and trips nothing, which is every stage
written before the field existed, and `no_kind` is exempt: it is not a word a project
declares.

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
  checks each stage's `is_grain_and_order_preserving` (read off `RowEffect`, the
  per-stage-type classification in `app/models/stages/stage_base.py`; the
  `python_row_function` type exists precisely so the runtime *enforces* the 1:1
  guarantee rather than trusting it); a non-preserving stage makes the eval
  non-scorable and the settings say why. A preserving stage with more than one
  input aligns along its FIRST input alone, so the walk also blocks where the
  dataset reached such a stage by a later one: an `enrich` emits one row per
  subject row, and a dataset injected on its reference side would be scored
  against rows it does not correspond to. This is why the function takes the
  dataset stage and the reference overrides as separate arguments — a flat list
  of both cannot tell which injection has to align.
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

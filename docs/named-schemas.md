# Named schemas + the eval data model

Two related pieces of `app/models/`: the named-schema data model (author tables
first, wire the DAG over them) and the eval model (grade a methodology against a
case table). Both exist on master as **validated Pydantic models**; neither is
yet consumed by the runtime, and no committed example exercises them end-to-end.
This doc describes the models as they are.

## Named schemas — the data model as a first-class artifact

The prototype began **DAG-first**: the data-model view was *derived from* the DAG
(it rendered each stage's inline `output_schema`). You could not model your data
without first authoring the pipeline. **Named schemas** invert this: the data
model can be authored as its own artifact, and the DAG wired over it.

Why it matters (the forcing example from the LobbyMap work): you cannot write a
benchmark-scoring stage until `query`, `data_source`, and `benchmark` exist as
tables — and nothing in the pipeline *produces* those; they're reference data. A
DAG-first tool has nowhere to put them.

The contract, in `app/models/named_schemas.py`:

- A **`NamedSchema`** is a `TableSchema` (columns + primary key) plus:
  - `name` — snake_case identity.
  - `kind` (`SchemaKind`) — where the table sits in the pipeline:
    - `reference` — dimension / lookup / benchmark data we must SOURCE, not
      compute (the "missing intermediate databases" of the forcing example).
    - `input` — raw data fetched into the pipeline.
    - `computed` — produced by a DAG stage.
    - `ground_truth` — external truth used only by eval.
- A **`NamedColumn`** may carry `references: <schema>` or `<schema>.<column>` —
  an explicit foreign key, making the data model a real graph rather than
  PK-name-collision guessing. `check_references_resolve` validates the graph.
- A **`SchemaLibrary`** is the set of a methodology's named schemas;
  `parse_schema_library` / `validate_schema_library` are the entry points.

**What is NOT here (yet):** no `schemas/` directory ships in any committed
example; the DAG's stages do not structurally import named schemas (an import
mechanism was built and deliberately reverted as premature — stages reference
schemas loosely, by intent). If you re-introduce coupling, make it loose first.

## The eval data model

Hard rule: **eval must not leak into the generation data model.** Generation has
no knowledge that eval exists; eval depends on generation, one-directional.

The contract, in `app/models/eval.py` (see its module docstring — it's the
authoritative description):

- An **`EvalConfig`** is the authored spec. Its core is ONE row-aligned table of
  cases: each row's `input_columns` are injected as `override_stage`'s output,
  and its `expected` columns are compared against `target_stage`'s output on the
  same row. Because it is a single table, input and expected are 1:1 **by
  construction**.
- That 1:1 alignment is only well-defined when every stage on the
  override→target path preserves grain (no fan-out/fan-in).
  `resolve_eval_run_settings` walks the path and checks each stage's
  `is_grain_preserving` (defined per stage type in `app/models/stage.py`); a
  non-preserving stage makes the eval non-scorable and the settings say why.
- **`StageOutputOverride`** injects a whole table as some stage's output,
  cutting that stage and everything upstream out of the run —
  `reference_overrides` use this to supply extra data a case needs.
- An **`EvalRun`** records the result at a specific methodology version:
  resolved settings, pass/fail, metrics, per-row results.
- Escape hatch: a `code` scorer replaces the declarative per-column comparison
  when it can't express the grading.

**What is NOT here (yet):** no runner integration (nothing executes an
`EvalConfig`), and no committed eval configs under `examples/`. Storage is
specified in [models-and-storage.md](models-and-storage.md)
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

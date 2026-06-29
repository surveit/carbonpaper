# Named schemas — the data model, authored before the DAG

This is the conceptual spine of the recent work. Read this before touching schemas
or the eval.

## The inversion

Originally the prototype was **DAG-first**: the data-model view was *derived from*
the DAG (it rendered each stage's inline `output_schema`). You could not author a
data model without first authoring the pipeline. **Named schemas** invert this: the
data model is a first-class artifact authored *first*; the DAG is wired *over* it.

Why it matters (the forcing example): you cannot write lobbymap's scoring stage
until `query`, `data_source`, and `benchmark` exist as tables — and nothing
*produces* those; they're reference data. A DAG-first tool has nowhere to put them.

## What a named schema is

A methodology declares a **library of named schemas** in
`examples/<name>/schemas/*.yaml` (one file may hold many schemas via multi-doc
YAML). A named schema = an anonymous `TableSchema` (columns + primary_key) **plus**:

- `name` — snake_case identity.
- `kind` — where the table sits in the pipeline. The distinction the DAG-derived
  view could not express:
  - `reference` — dimension / lookup / benchmark data we must SOURCE, not compute
    (the "missing intermediate databases").
  - `input` — raw data fetched into the pipeline.
  - `computed` — produced by a DAG stage.
  - `ground_truth` — external truth used only by eval (lives in `eval/`, not here).
- per-column `references: <schema>` or `<schema>.<column>` — an explicit FK,
  making the data model a real graph (not PK-name-collision guessing).
- `exclusive_arcs: [[col_a, col_b]]` — a validated **XOR foreign key**: exactly one
  of the listed (nullable) columns is set per row. e.g. lobbymap's `cell_score`
  scores a company XOR an influencer.

Contract + validators: `app/dag_schema.py` (`SCHEMA_KINDS`, `validate_named_schema`,
`validate_schema_library`). View: `/methodology/{m}/schemas`
(`schema_library.html`), which renders the model grouped by kind, a validation
banner, and a scroll/zoom/pan ER diagram built from the explicit `references`.

## The two-step authoring process (the intended workflow)

1. **Author the data model** (`schemas/`) — the typed tables and their FK graph.
2. **Author the DAG** (`compiled/`) — stages that consume/produce those schemas.

Step 2 currently references the schemas loosely (by intent/name), NOT structurally:
an earlier "import" mechanism (materialize a named schema into a stage at load
time) was built and then **deliberately reverted** — the team chose a loose
reference for now; structural import was deemed premature. If you re-introduce
coupling, make it loose first.

## The eval data model (separate from generation, consistent by construction)

Hard rule: **eval must not leak into the generation data model.** Generation has no
knowledge that eval exists; eval depends on generation, one-directional.

- Eval specs live in `examples/<name>/eval/*.eval.yaml`.
- An eval spec names the generation schema it grades (`evaluates:`) + the columns
  it mirrors (`mirror_columns`) + eval-only `extra_columns` + `metrics`.
- The ground-truth schema is **derived** from the generation schema via
  `build_ground_truth_schema` — the mirrored columns ARE the generation columns, so
  ground truth is consistent **by construction** and cannot silently drift.
  `validate_eval_spec` enforces the spec grades real columns.

Example: lobbymap's `eval/cell_score.eval.yaml` derives `gt_cell_score` from the
generation `cell_score` (+ an `evidence_url` provenance column), inheriting its
`exclusive_arcs`.

## Authoring tips / gotchas

- Don't enumerate data you don't have (e.g. the full query list) — model the table,
  leave rows to be sourced; note the gap in `notes:`.
- `range:` on a column = numeric `[low, high]` or an enum of allowed strings.
- A `-2..+2` score plus `NA`/`NS` does NOT fit one numeric column — model a
  `status` enum + a nullable `score` (absence ≠ zero). lobbymap's `cell_score`
  does this; it's a recurring trap.
- Separate entity tables give cross-kind FK safety for free; a single
  `entity{kind}` table needs an `exclusive_arc`/validator to get it back.

# Eval UI — design

Date: 2026-07-04. Status: draft for review.

## Purpose

The eval data model (PR #28, `app/models/eval.py`) landed on master with no UI. This
design adds the eval surface to the methodology review app: viewing eval configs and
their runs, authoring new configs, and surfacing eval state while reviewing the
methodology graph.

**v1 scope: view + author.** No eval execution — the scorer runtime (running an
`EvalConfig` through the executor and writing an `EvalRun`) is a separate, later
milestone. Until it lands, the only observable eval states are "never run" and
"broken" (defined below); the UI defines the full status vocabulary now and the
remaining states light up when runs exist.

## Definitions

Terms used throughout; the model objects are defined in `app/models/eval.py`.

- **Eval config** (`EvalConfig`): the authored spec. One row-aligned table of cases;
  each row's `input_columns` are injected as the **override stage**'s output, and its
  `expected` columns are compared against the **target stage**'s output on the same
  row, joined on `key` columns. A config may also carry `reference_overrides` —
  further tables injected as other stages' outputs. An eval can therefore pin many
  stages' outputs, but the cases table itself feeds exactly one override stage.
- **Pathway**: the overridden stages, the stages that execute downstream of them, and
  the target — what one eval exercises. `resolve_eval_run_settings` computes the
  executing set.
- **Compatibility**: whether a config still fits a given methodology version — its
  stages exist, their output schemas cover what the eval injects and asserts (the
  five-condition check below). Computed, never stored.
- **Eval run** (`EvalRun`): one scoring result, pinned to a `methodology_version`.
- **Status vocabulary** (per eval, derived at render time, in worst-first order):
  1. **broken** — config incompatible with the current methodology version
  2. **run errored** — latest run was at the current version and ended in `error`
     (or `vetoed`: not declaratively scorable and no code scorer supplied)
  3. **stale** — latest run predates the current version; current result unknown
  4. **never run** — compatible, no runs exist
  5. **run succeeded** — latest run was at the current version and scored. Success
     means "produced a result", not "the result is good": a run can succeed with
     terrible metrics, so headline metrics are always displayed alongside the
     status, and the scorer's `passed` bool is presented as the scorer's judgment,
     not as a UI-level green/red verdict.

  No result is displayed as current unless its run's pinned version equals the
  current version — a green marker from a stale run is a fabricated claim.

## Information architecture

Evals are a section of the methodology surface, entered from the button-row nav on
the methodology page (today: `Versions · Data model · All runs`; add `Evals`).

Routes:

- `/methodology/{m}/evals` — **Evals home**: one table row per config — name, span
  rendered as `override → target`, status, last run + its pinned version. Plain
  typographic rows.
- `/methodology/{m}/evals/{id}` — **config page** (anatomy below).
- `/methodology/{m}/evals/{id}/runs/{run}` — **run page**: status, metrics, per-row
  result table (reuses the full-table component from the run-entries view), and the
  pinned `methodology_version` with a stale marker when it is no longer current.
- `/methodology/{m}/evals/new` and `/methodology/{m}/evals/{id}/edit` — one shared
  authoring form (create mode empty, edit mode prefilled from the config).

Deliberately absent: a Tables section or table registry. Eval datasets are files —
`TableRef` (path + format + declared schema) is the whole concept, and organization
is a read-side problem (pickers enumerate paths that already appear in the system).
Eval runs do NOT appear in the Runs section: Runs is production monitoring, evals
are validation; cross-links only.

## Config page anatomy

1. **Pathway** — a mini-graph excerpt highlighting the overridden stages (dashed,
   "injected"), the executing stages, and the target (thick border, "compared").
2. **Status panel** — the compatibility check result against the current version,
   with the failing condition named when broken (e.g. "target no longer emits
   `alignment_score`"), plus the grain-gate result (`can_score_declaratively`, and
   the blocking stages when false).
3. **Cases** — the dataset table (input columns | key | expected columns), full-table
   component.
4. **Scoring** — the `ExpectedColumn` rules (metric, tolerance), rollup metrics, code
   scorer if present.
5. **Run history** — runs by pinned version, newest first. This is the regression
   view; meaningful precisely because the config is version-independent while runs
   are version-pinned.

## DAG overlay

An eval tests a pathway, not a node, and one methodology may have many evals — so the
graph and the status display split duties:

- **Graph shows geometry.** Default state: a warning marker on every stage that is on
  NO eval's pathway — uncovered is the state that demands the reviewer's attention,
  so absence of coverage is what gets flagged, not presence. No status colors on
  nodes.
- **Popover shows status.** An `Evals (n)` button in the methodology page's button
  row opens a popover: list rows (status dot + name + `override → target` span in
  monospace), styled as text rows so they cannot be mistaken for graph nodes.
- **Selection binds them.** Clicking a row pins that eval's pathway highlight on the
  graph (dashed overridden stages / filled executing stages / thick-bordered target)
  and shows a
  dismissable "pathway: {name}" pill so the highlight survives the popover closing.
  A second activation navigates to the eval's page.

Mechanics: the page already renders mermaid client-side with deterministic node ids;
the template embeds a JSON map of eval → {overridden stages, executing stages,
target, status} and a small JS layer toggles CSS classes on the rendered SVG. No
re-render per selection.

The stage page gets an "evals touching this stage" list (each naming the stage's
role: overridden / executes / target).

## Compatibility check

One pure function, the single source of truth:

```
check_eval_compatibility(config: EvalConfig, methodology: Methodology) -> report
```

Conditions, against a given methodology version:

1. `override_stage`, `target_stage`, and every `reference_overrides` stage exist.
2. Every injected table is a valid stand-in: for the cases-table override and each
   reference override, the injected columns cover that stage's declared
   `output_schema`, by name and type.
3. The assertion columns exist: every `ExpectedColumn.actual` is in the target's
   `output_schema`, type-compatible with its metric (`abs_tol` requires numeric).
4. Alignment holds: `key` columns exist in both the dataset and the target's
   `output_schema`.
5. Grain holds: `resolve_eval_run_settings` (already in the model) — or, when it
   reports blocking stages, a code scorer is present.

The report names the failing condition in plain terms. Conditions 1 and 5 exist in
the model today; 2–4 are new logic shipped with this UI.

Call sites (all three surfaces agree because they call the same function):

1. **Eval page render** — computed on render, not persisted. A stored broken-flag
   needs invalidation logic and can drift; recomputing from (config, current stages)
   is cheap and cannot lie.
2. **DAG popover / evals home** — feeds the status vocabulary.
3. **Methodology write path** — any handler that writes stages runs the check for
   every config of that methodology and returns the breakage list in its response
   ("this edit breaks eval `spot-checks`: condition 3 …"). Breakage is flagged at
   cause time, and it **warns, never vetoes** — evals are claims that may
   legitimately need updating when the methodology evolves. Note: master currently
   has no methodology write path (stages are YAML read from disk); this ships as the
   function plus wiring instructions, and the demo-line staged-edit Save becomes its
   first caller when that stack rebases onto master.

## Authoring form

One form component serves both create (`/evals/new`) and edit
(`/evals/{id}/edit`, prefilled from the stored config). Flow:

1. **Pick override and target stages.** Pickers list stages; a stage without a
   declared `output_schema` is shown disabled with the reason ("declares no output
   schema") — visible, never silently hidden. Eligibility is presence-based, not
   stage-type-based: no declared output schema, not selectable.
2. **Requirements derive from the chosen stages.** The user never authors the
   dataset's schema: required input columns + types are read off the override
   stage's `output_schema`; legal `actual` columns + types off the target's. (Same
   move the earlier `EvalSpec.build_ground_truth_schema` made: derive from what is
   graded, so it cannot drift.)
3. **Supply the file** — a repo-relative path, or an upload landing in a dedicated
   directory (proposal: `eval_data/`, repo-relative). The app never overwrites an
   existing file: immutability is a convention on bare disk (enforced storage comes
   with a server later). Live grain-gate + compatibility feedback on every picker
   change. Reference overrides, when needed, follow the same pattern: pick a stage,
   supply a file, validated against that stage's `output_schema`.
4. **Map columns** — file column → required input column; dataset column →
   `ExpectedColumn.expected` for each asserted `actual`; pick `key` columns.
5. **Validate the bytes** — the file validator loads the file and checks it against
   the derived schema; mismatches fail loudly with specifics. No inferred types
   presented as fact.
6. **Save** mints the `EvalConfig`; `TableRef.table_schema` is filled mechanically
   from the mapping. Objects stored per the storage convention
   (`eval_config/<id>.data`).

The **file validator** (load file, check against a declared `TableSchema`, loud
errors) is the one shared service this design adds; input stages can adopt it later.

## Assumption: output schemas are present

This design assumes every stage with a downstream consumer declares `output_schema`.
Audited 2026-07-03: true for all 31 stages across congresswatch / drift / lobbymap
`compiled/` dirs (including terminals). **No methodology validator is added by this
design** — that area is deliberately left untouched. Where the assumption fails
anyway, the UI degrades explicitly: stage pickers disable the stage, and the
compatibility check reports "cannot verify" rather than passing.
(`examples/lobbymap/compiled_v1/` is a legacy pre-schema format that does not parse
against the current model; flagged separately for archival, not part of this design.)

## Build order

1. Compatibility check function + file validator (pure logic + tests).
2. Eval read pages (home, config, run) + DAG overlay.
3. Authoring form, create + edit modes (consumes 1).

1 has no UI dependencies; 2–3 depend on it.

## Out of scope (deliberate)

- Scorer runtime / eval execution and the run-triggering button.
- Promotion of run outputs to eval datasets (later affordance; decision recorded:
  copy-on-promote, schema carried from the stage's `output_schema`, source recorded
  as "run X, stage Y" — because run directories get cleaned and re-run).
- Any Tables section, table object type, or registry.
- Coverage-dimming toggle on the DAG (the tint covers v1).
- Persisting save-time breakage warnings beyond the save response. If a durable
  record ("broke at version v12") is wanted later, it is derivable by running the
  check against stored versions.
- Methodology validators of any kind (schema presence is assumed, not enforced).
- Marking runs in the history that predate the latest config edit — deferred until
  the scorer runtime exists and runs are real.
- Changes to `EvalConfig`, `Stage`, or `input_data` stages.

## Testing

- Unit: compatibility conditions 2–4 (each failing condition named; reference
  overrides included), file validator (type mismatches, missing columns, empty
  files).
- Route tests: eval pages render for a methodology with configs on disk; home shows
  correct statuses for broken / never-run fixtures; the shared form renders empty in
  create mode and prefilled in edit mode.
- The overlay JS is exercised by a fixture page test (embedded JSON map present,
  classes toggle); visual behavior verified manually against the lobbymap example.

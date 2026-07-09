# Eval authoring redesign

Status: design approved 2026-07-04. Revises the authoring flow shipped in PR #59
(branch `eval-ui`), before that PR merges. Folds into the same branch.

## Why

Three problems with the authoring form as first built, all surfaced in review of
the running app:

1. **Override and target are picked from dropdowns divorced from the graph.** The
   whole framing is "an eval tests a *pathway*", and the methodology page already
   draws that graph and highlights eval pathways on it — but creating an eval
   ignored the graph and used two `<select>` boxes.
2. **Nothing rejects an unreachable pathway.** If the override stage is not
   upstream of the target stage, injecting the override changes nothing on the
   path to the target, so the eval is inert. Today this saves without complaint:
   `resolve_eval_run_settings` walks the target's ancestors and simply never
   encounters the override, producing a settings object as if all were well.
3. **The cases file is required, and "expected columns" is opaque.** The config
   is coupled to a specific data file it does not conceptually depend on, and the
   central concept (the assertions being graded) carries a confusing label.

## What this changes — and what it does not

**Unchanged** (all still correct, all keep their tests): the read pages
(index / config / run), the pathway overlay on the methodology graph, the eval
store's file layout, `TableRef`, and every existing condition inside
`check_eval_compatibility`.

**Changed**: the `EvalConfig` model (one field becomes optional), the
compatibility function (one new condition, plus tolerating a missing file), the
derived status (one new value), and the authoring page (graph selection, a
renamed concept, and a deferrable file).

Definitions used below:
- **pathway** — the override stage, the target stage, and the stages that
  execute between them. An eval grades one pathway.
- **override stage** — the stage whose output the eval replaces with fixed
  values from each case row (the injected inputs).
- **target stage** — the stage whose output the eval grades.
- **check** — one assertion: a column the target stage emits, compared against a
  column of correct answers in the cases file, using a comparison rule
  (`exact` / `abs_tol` / `sign`). This is the concept the form previously called
  "expected columns". In the model it remains `ExpectedColumn`; only the UI label
  changes to "check".

## Model change: the cases file becomes optional

`EvalConfig.table` changes from `TableRef` (required) to `Optional[TableRef]`,
default `None`. Nothing else on the model changes: `key`, `input_columns`, and
`expected` stay required and non-empty, because they define the eval — they name
the columns the cases file will have to supply and the assertions to grade. The
file supplies *rows* for a schema the config already fully determines; it does
not define the eval.

A config with `table = None` is a complete, valid definition with no data yet.
The cases file is attached later (see "Attaching cases").

## Compatibility change: reachability, and tolerating no file

Two edits to `check_eval_compatibility(config, stages)` in
`app/services/eval_compat.py`:

1. **New condition — the target must be reachable from the override.** After the
   "every referenced stage exists" condition, compute the set of stages
   downstream of `override_stage` (its descendants by following inputs forward).
   If `target_stage` is not among them, add a problem:
   `` "target `<t>` is not reachable from override `<o>`; the override would not
   affect it" `` and mark the report not-ok. Reference overrides are exempt (they
   inject side data and need not lie on the pathway). This is what makes the
   graph-selection UI's live dimming (below) an enforcement, not just a hint.

2. **Tolerate `table = None`.** The three places that read
   `config.table.table_schema` — the cases-table coverage check (condition 2),
   the `key ∈ dataset columns` check, and the grain-path setup — must guard for a
   missing table. When `table is None`, skip exactly the checks that need the
   file's schema (cases-table coverage; key-in-dataset). Keep the checks that do
   not need the file: referenced stages exist, target emits each asserted column,
   `abs_tol` needs a numeric target column, key is emitted by the target, the
   reachability condition, the structural check, and the grain condition. So a
   dataless eval is still fully checked for everything except "does the file
   match", which is unanswerable until a file exists.

The report stays computed-on-demand and unstored, as before.

## Status change: "no cases yet"

`eval_status` gains one value, ranked directly below `broken`:

```
broken          — compatibility failed (now includes an unreachable pathway)
no cases yet     — compatible, but table is None; nothing to run          [NEW]
never run        — has cases, no run recorded
stale            — newest run pinned a different methodology version
run errored      — newest current run has status error or vetoed
run succeeded     — newest current run produced a result
```

`eval_status` takes a new `has_cases: bool` (true when `config.table` is not
None); the four templates that render a status badge map the new value to the
neutral `pending` badge class. (Collapsing those four duplicated badge maps into
one Jinja macro is a noted follow-up, out of scope here.)

## Authoring page: select on the graph, defer the file

One page, `GET /methodology/{m}/evals/new`, restructured into three regions that
fill in progressively. Server-side validation on POST remains the source of
truth; the JS only drives selection and populates pickers.

**Region 1 — pick the pathway (on the graph).** The page renders the methodology
graph (reusing `build_mermaid_graph`). The page also embeds, as JSON, a
descendants map computed server-side from the stages (each stage → the stages
reachable downstream of it). Interaction:
- First click selects the **override** stage; it highlights, and every stage not
  in its descendants dims (they cannot be a valid target).
- Second click selects the **target** stage (only a non-dimmed node is
  accepted). The pathway between them highlights.
- A "clear" affordance resets the selection.
- Override and target are written to hidden form fields.

Click order is override-then-target (matching data-flow direction); the live
dimming makes an unreachable pathway unselectable, so the new reachability
condition in `check_eval_compatibility` is a backstop rather than the primary
guard. (The order is
trivial to flip if it reads better target-first in use.)

**Region 2 — name and checks.** Appears once both stages are picked. Fields: id
(create only; slug), name, description. Then the **checks** builder — each check
has: a target output column (a picker populated from the target stage's
`output_schema` via the existing `GET .../evals/stage-schema/{id}.json`
endpoint), the name of the cases-file column holding the correct answer, a
comparison rule (`exact` / `abs_tol` / `sign`), and a tolerance when the rule is
`abs_tol`. Key columns and input columns are selected here too, from the relevant
stage schemas, exactly as the current form derives them. No dataset types are
ever hand-authored — they come from the stage schemas.

**Region 3 — attach cases (optional).** A clearly-optional trailing section:
upload a CSV now, or save without one. Copy states plainly that cases can be
added later. On save:
- If a file is provided, it is uploaded (`save_dataset_upload`, 409 on name
  collision) and validated against the derived schema exactly as today; the
  config stores its `TableRef`.
- If no file is provided, the config saves with `table = None` and lands at
  status "no cases yet".

## Attaching cases later

The eval config page (`/methodology/{m}/evals/{id}`) gains, for a dataless eval,
an "attach cases" affordance in place of the cases table: upload a CSV, validated
against the same derived schema, which sets `table` and re-saves the config.
This reuses the upload + validate path from the authoring POST; it is a small
handler, not a new page. Replacing an existing cases file is out of scope for
this pass (files are immutable by convention; a replace flow can come later).

## Out of scope (explicit)

- **Hand-entering case rows.** An editable grid is real work; upload-a-file is the
  only way to attach cases in this pass. The model already supports adding cases
  later, so row-by-row entry is a clean future addition.
- **A code-scorer authoring path.** Non-grain-preserving pathways remain flagged
  by the existing grain condition; authoring a `CodeScorer` from the UI is not
  added here.
- **Replacing an existing cases file.**
- **The scorer/executor runtime** (unchanged from PR #59's scope — no eval
  actually runs yet).
- **Collapsing the duplicated status→badge map into a macro** (noted follow-up).

## Verification

- New unit tests: reachability rejection (unreachable override→target is
  `broken`); `check_eval_compatibility` with `table = None` returns a report
  (not a crash) and still catches a target-schema assertion error; `eval_status`
  returns "no cases yet" for a compatible dataless config; save-without-file
  persists `table` absent and the config page renders the attach affordance;
  attach-cases sets `table` and flips status off "no cases yet".
- Graph-selection JS is browser-only; a human verifies click-to-select,
  reachability dimming, and the pathway highlight on the real rendered graph.
- Full suite, ruff, mypy strict stay green.

# Eval model: drop `key` and `input_columns`, align by lineage

Status: decided 2026-07-04, same session as the authoring redesign
(`2026-07-04-eval-authoring-redesign.md`). A further model simplification on the
`eval-ui` branch (PR #59), reached by working the alignment question through.

## Decision

Remove two authored fields from `EvalConfig`:

- **`input_columns`** — fully derivable. An eval replaces the override stage's
  *entire* output, so the columns a case injects are exactly that stage's
  output-schema columns; there is no meaningful subset (you cannot partially seed
  a stage — downstream reads the whole output). Nothing grades on it. Storing it
  is also a drift hazard: a stored copy of a derivable value goes stale if the
  stage schema changes — the same reason compatibility is computed, never stored.

- **`key`** — unnecessary once alignment is by lineage (below).

The authored eval is then: `override_stage`, `target_stage`, the checks
(`expected`), an optional cases `table`, plus `reference_overrides` / `code`. The
cases file's columns are **the override stage's output columns (to inject) + each
check's expected-answer column (to grade)** — nothing else.

## How rows align: lineage, not a key, not position

An eval run injects the cases table as the override stage's output and runs the
grain-preserving path to the target. To grade, each target output row must be
matched back to the case that produced it. Three options were considered:

- **A data `key` column** (join actual↔expected on `doc_id` etc.). Works, but the
  identity it provides is already present in the injected input columns, and it
  is an extra authored field for something the run can determine itself.
- **Row position** (i-th case → i-th target row). Fails silently if any row is
  dropped or reordered mid-path: every later row shifts and mis-scores. Rescuing
  it requires null-filling dropped rows to hold positions — which fabricates a
  row where the pipeline produced nothing. Rejected.
- **Row-level lineage** (chosen). The runtime stamps each injected case row with
  an internal id; the lineage tracer carries it to the target; scoring aligns on
  that id. A case whose row errors or drops simply has **no** matching target row
  and is scored as *no result* for that case — attributed to the exact case, with
  no invented null row. Robust to drops without null-filling, and needs no
  human-supplied key.

This is why `key` goes: lineage does the alignment a key column used to do, more
robustly, using an id the user never authors.

## Scope note: fan-out / fan-in stays out

The declarative path remains grain-preserving only. Evals over a fan-out or
fan-in target need a code scorer (a real-engineer / different-customer concern,
possibly LLM-drafted) — they are explicitly out of the declarative scope, so the
"aggregate graded on a domain key" case never arises here. That case was the only
one that would have re-introduced a key; it is out of scope, so it does not.

## Contract for the future scorer (deferred runtime, not built here)

Recorded so the executor, when built, implements alignment consistently:

- The eval pathway must be **grain-preserving** (already gated by
  `Stage.is_grain_preserving`; join / aggregate / frame-function excluded), so
  each case maps to at most one target row.
- Align the target output to the cases by **lineage row-id**, not by position or
  a data key.
- **Do not null-fill.** A case with no matching target row (its row errored or
  was dropped) scores as *no result* for that case — never a fabricated null row.
- This depends on the row-level lineage tracer (the show-your-work rows-in/
  rows-out work, currently a separate branch) being present in the eval runtime.
  Accepted dependency: no eval can score until that lands, and nothing scores
  today regardless.

## What changes now (model + surface only; no runtime)

- `app/models/eval.py`: remove `key` and `input_columns` fields and their
  non-empty validators; update the module/class docstrings (checks define the
  eval; alignment is by lineage; cases file = override cols + expected cols).
- `app/services/eval_compat.py`: remove the `key`-based conditions (key in the
  cases table; key emitted by the target) and the now-unused `dataset_cols`
  derivation. Keep: referenced stages exist, reachability, the override-output
  coverage check, each check's `actual` column exists on the target (numeric for
  `abs_tol`), the structural check, and the grain condition.
- `app/web/routers/evals.py` + `app/templates/eval_form.html`: remove the `key`
  and `input_columns` pickers and their form parsing; the derived required-cases
  columns become override output columns + each check's expected column.
- Tests: drop `key` / `input_columns` from every `EvalConfig` fixture; update the
  compat and web tests to the new shape.

## Verification

- `EvalConfig` validates with no `key` / `input_columns`; a stray `key=` is not
  silently accepted as data (relies on the model's existing extra-field policy).
- `check_eval_compatibility` no longer references `key` / `input_columns`; its
  other conditions (reachability, override coverage, check-column existence,
  grain) still fire on the same inputs as before.
- The authoring form renders and submits with no key / input-columns fields; a
  created eval's cases-schema requirement is override cols + expected cols.
- Full suite, ruff, mypy strict stay green.

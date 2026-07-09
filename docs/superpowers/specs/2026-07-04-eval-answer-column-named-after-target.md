# Eval checks: the answer column is named after the target column

Status: decided 2026-07-04, same session as the authoring redesign. Follows the
same "don't author what's derivable" line as dropping `key` / `input_columns`
(`2026-07-04-eval-drop-key-lineage.md`).

## Problem

A check compared a target output column (`actual`) against a *separately named*
cases-file column (`ExpectedColumn.expected`, surfaced in the form as an "answer
column" / "dataset column" text field). That indirection was confusing and
error-prone: the user typed a name, the derived schema/template demanded that
exact name, and an uploaded file that didn't match failed validation for a reason
that read as a system bug.

## Decision

A check is just **which target column to check** plus how to compare it. The
cases file's answer column is named **the same as the picked target column** —
there is nothing separate to author.

- `ExpectedColumn` drops its `expected` field. It becomes `{actual, metric,
  tolerance}`.
- For a check on target column `C`, the cases file's answer column is named `C`.

## Name-clash rule

The cases file also carries the override stage's output columns (the injected
inputs). If a check's target column name `C` equals one of those injected
column names, a single flat table can't hold two `C` columns. Auto-disambiguate:

- the injected input is written as **`override.C`**,
- the answer is written as **`output.C`**,
- and a **warning** is surfaced (in the schema preview / on the config page) so
  the user knows the columns were auto-renamed to avoid the clash.

Only the clashing name is prefixed; every non-clashing column keeps its plain
name. The set of clashing names is `override_output_columns ∩ {check target
columns}`.

Rationale: "if I pick a column, that's the name I upload." The answer-column name
is derived from the check, never authored or stored — so it can't drift and can't
mismatch the file the template hands you.

## Scorer contract (future runtime, not built here)

The naming convention is reversible, which is what the (deferred) scorer needs:
`override.C` in the cases file injects as the override stage's `C` output;
`output.C` is the expected value for the target's `C` column; unprefixed columns
are override inputs (when not a check target) or answers (when a check target,
no clash).

## What changes now (model + surface; no runtime)

- `app/models/eval.py`: remove `ExpectedColumn.expected`; update its docstring
  (a check names a target column to grade; the answer column is derived).
- A single **clash-aware column derivation** (shared, one source of truth) that,
  given the override stage's output schema and the checks' target columns,
  produces the ordered cases-file columns (injected + answer, with `override.` /
  `output.` prefixes applied only on a clash) plus the warnings. Used by:
  - `_derive_table_schema` (the schema preview + template),
  - `check_eval_compatibility`'s override-coverage check (so it validates an
    uploaded file against the *same* possibly-prefixed names), and
  - the `cases-schema` endpoint (returns the warnings for the preview).
- `app/templates/eval_form.html`: remove the answer-column input from each check
  row; a check row is now target-column picker + metric + tolerance. The schema
  preview shows the derived column names and any clash warning.
- Tests: drop `expected` from every `ExpectedColumn` fixture; add coverage for
  the no-clash case (answer named after target) and the clash case
  (`override.C` / `output.C` + warning).

## Verification

- `ExpectedColumn` validates with no `expected`.
- The derived cases schema for a check on target `C` contains an answer column
  `C` (no clash) or `output.C` with the injected `override.C` and a warning (on
  clash); never a duplicate column name.
- `check_eval_compatibility` accepts an uploaded file whose columns follow the
  derived (possibly prefixed) names and rejects one missing them.
- Full suite, ruff, mypy strict stay green.

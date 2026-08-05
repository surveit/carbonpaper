# The llm_transform output rules (design decision, 2026-07)

*Decided in review while simplifying palm_tier2's unit-validation chain. The goal:
the JSON an LLM stage must return is **computable from its schemas**, so prompts stop
hand-writing (and drifting from) output shapes. The set of columns the reply must
carry — with their per-column type/nullability/enum/range and, for structured
columns, their recursive shape — is computed from the stage's own input edge and
resolved output schema (the difference between them) and rendered into the prompt,
rather than written by hand. Validating the reply against that shape and guaranteeing it is a
separate concern that belongs to the LLM layer, planned for a later PR (see Status).*

## Rules

1. **1:1 and append-only.** An `llm_transform` maps one input row to one output row,
   and may only ADD columns. The LLM's expected JSON object is exactly the columns
   the stage's `signature` adds — the difference between its resolved output schema
   and its input edge. Passthrough columns
   are copied by the runtime, never round-tripped through the model (the LLM cannot
   mutate identity fields, and tokens aren't wasted re-emitting them).
2. **1:N is expressed as one added JSON *array* column** — an array of scalars or of
   *flat* records. Fan-out lives in a value, not in row multiplication by the LLM.
   If downstream needs the array tabular, explosion is a separate mechanical step,
   computable from schema: the exploded stage gains one identity column, and that
   column names the record key that drives the explosion.
3. **A `json` column must declare its shape recursively, in our own schema
   language** — not a raw draft-07 blob. A `json` (or `list[json]`) column carries
   either `Column.fields` (a nested list of `Column`, so an object's shape is
   described by the same primitives as a table, recursively) or `Column.value_type`
   (an open map: arbitrary string keys whose values are one scalar type). A `json`
   column declaring neither, or a non-json column declaring either, is a schema
   error. This keeps nested values self-describing rather than opaque blobs. (The
   `dict` type is gone; there is one structured type, `json`.)
4. **No revise-in-place.** A stage's output table is the review surface; overwriting
   a column destroys history exactly where review happens (the upstream table still
   exists, but the reviewer of *this* table can no longer see what changed). A
   re-judgment writes a new column (`score` → `revised_score`). This also keeps rule
   1's set-difference exact — no "revises" markers, no exceptions.

## What this buys

- **The prompt's output instructions are generated, not authored**: types,
  nullability, enums, numeric ranges, and a `json` column's recursive shape already
  declared on the added columns go straight into the prompt via `TableSchema.subtract`
  + `TableSchema.to_prompt`. No hand-written "return JSON shaped like…" that drifts
  from the schema.
- **The JSON-output guarantee is a separable LLM-layer concern.** Validating a reply
  against the computed shape, re-asking on violation, and dropping (not nulling) a row
  that still fails after N tries belongs to an agent that "guarantees a specific JSON
  output," in the LLM layer — not to `llm_transform`. That layer, when built, feeds
  back the specific schema errors only; epistemic guidance (when to return null,
  "never fit a number to a range") belongs in the stage's own authored prompt (the
  compiler lane), not in a correction message. See Status for what exists today.
- **Group-input (N:1) is not an llm_transform concern**: grouping is deterministic
  (a collate/aggregate stage); the LLM then runs 1:1 over grouped rows.

## Status

This describes what the code does today, not an aspiration:

- **Rule 1 is enforced by `Stage` construction, not in the handler.** Two
  validators in `app/models/stages/llm_transform.py` split it: the 1:1 validator
  (`find_llm_one_to_one_issues`) requires exactly one input and at least one
  added column, and the signature check (`find_llm_signature_issues`) refuses a
  `rewrites` entry. Keeping every input column unchanged comes for free — an
  `extends` signature flows every anchor column, so the output cannot drop one.
  Because a stage carries its own contract, an ineligible stage can't be built —
  so it can't be loaded, versioned, or run — and `TableSchema.subtract` (the
  resolved output minus the input edge) is exactly the reply columns and can
  never throw when the runtime computes it.
- **The reply spec goes into the prompt; the call machinery is unchanged.**
  `make_llm_row_mapper` appends `subtract(...).to_prompt()` to the stage's
  prompt and calls `llm.call_llm` per row, driven by the runtime's row driver
  (`app/runtime/stages/execution.py`) — the same call path as any other
  row-mapped stage. Appending the computed spec is the only thing this mapper
  adds over a plain LLM call.
- **There is no reply validation, retry, transcript, or JSON guarantee here.**
  Validating a reply against the computed spec, re-asking on violation, dropping
  (not nulling) a row that still fails, and persisting per-row conversations are
  planned for a later PR that unifies the LLM-calling agent in `app/chat`. This
  PR changes prompts only.
- **Rule 2's mechanical explosion is NOT implemented**, and a 1:N array column
  does not get you automatic row explosion; there is no dedicated fan-out stage
  type yet.
- **Rule 4 is a modeling convention**, not something the runtime enforces
  mechanically; nothing currently checks that a stage avoids revise-in-place.

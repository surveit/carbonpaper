# The llm_transform output rules

What an `llm_transform` stage may do to its frame, and what the JSON its model
returns must carry. The reply spec is read off the stage's own `signature`, so a
prompt never hand-writes — and never drifts from — an output shape.

## Rules

1. **1:1 and append-only.** An `llm_transform` maps one input row to one output
   row and may only ADD columns. The JSON object the model must return is
   exactly the columns in `signature.adds`. Every other input column flows
   through untouched, rejoined by the runtime rather than round-tripped through
   the model — so the model cannot mutate an identity field, and no tokens are
   spent re-emitting one.
2. **1:N is expressed as one added JSON *array* column** — an array of scalars
   or of *flat* records. Fan-out lives in a value, not in row multiplication by
   the model. Nothing turns such an array into rows on its own; where downstream
   needs it tabular, a `python_frame_function` does that as its own stage.
3. **A `json` column states its shape recursively, in our own schema
   language** — not a raw draft-07 blob. A `json` (or `list[json]`) column
   carries exactly one of `Column.fields` (a nested list of `Column`, so an
   object's shape is described by the same primitives as a table, recursively)
   or `Column.value_type` (an open map: arbitrary string keys whose values are
   one scalar type). Neither, both, or either one on a non-json column is
   refused by `Column._json_shape`. There is one structured type, `json`.
4. **No revise-in-place.** A stage's output table is the review surface;
   overwriting a column destroys history exactly where review happens (the
   upstream table still exists, but the reviewer of *this* table can no longer
   see what changed). A re-judgment writes a new column (`score` →
   `revised_score`). For this stage type the rule holds by construction —
   `find_llm_signature_issues` refuses a `rewrites` entry. Other stage types may
   rewrite; there the rule is a modeling convention only.

## Where each rule lives

- **Rule 1 is enforced by `Stage` construction, not in the handler.**
  `LLMTransformStage` declares `signature: ExtendsSignature`, and an `extends`
  signature flows every anchor column, so keeping the input intact comes for
  free — the output cannot drop a column. `find_llm_signature_issues`
  (`app/models/stages/llm_transform.py`), reached through
  `StageBase._signature_consistent` like every other type's config check,
  requires exactly one input and at least one entry in `adds` (a signature that
  adds nothing asks the model for nothing), refuses `rewrites`, and holds the
  reads and the prompt's `{placeholder}`s to naming the same columns. An
  ineligible stage cannot be built, so it cannot be loaded, versioned, or run.
- **The reply spec is `signature.adds`, read directly.** Both execution paths in
  `app/runtime/stages/llm_transform.py` build
  `TableSchema(columns=signature.adds)` and compile it with `to_pydantic_model`:
  `make_llm_row_mapper` for `batch_size: 1`, and `_build_batch_reply_schema` for
  the batched path, where each item additionally carries the runtime-assigned
  `row_number` that rejoins it to its row. The type, nullability, enum, numeric
  range and recursive `json` shape already declared on each added column
  therefore reach the model as the reply schema itself. The stage panel shows a
  reviewer that same list — `stage.signature.adds`, under **expected answer
  shape** in `_stage_executable.html`.
- **The reply is validated by construction.** `call_llm` runs an
  `app.core.agent.agent.Agent` whose `target_schema` is that compiled model, so
  a schema-invalid reply is re-asked inside the agent's own loop rather than
  parsed out of prose. A row that never yields a valid reply is tagged with the
  `_error` sentinel and surfaces as an error-severity output issue; the row
  survives carrying nulls, and nothing is fabricated. Epistemic guidance (when
  to return null, "never fit a number to a range") belongs in the stage's own
  authored prompt, not in a correction message.
- **Group-input (N:1) is not an `llm_transform` concern**: grouping is
  deterministic — an `aggregate` stage — and the model then runs 1:1 over the
  grouped rows.

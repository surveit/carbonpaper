# The pandas seam

Where pandas is allowed to exist, and why it is allowed to exist there.

## Terms

- **Frame** — one table of rows moving between stages. Today it is a
  `pandas.DataFrame` everywhere; the direction of travel is a `pyarrow.Table`
  everywhere except the seam below.
- **Authored code** — Python a user of carbonpaper wrote and stored in their own
  project: the `function:` block of a `pandas_frame_function` or a `publish`
  stage. It is *their* code, not ours, and it is stored data.
- **The seam** — the boundary where the runtime materializes a pandas frame to
  hand to authored code, and coerces what comes back against the stage's
  declared output schema.
- **`object` dtype** — pandas' fallback for a column with no fixed-width type (a
  list, a dict, mixed types). It does not approximate the type; it erases it.
  A column of `object` may hold any Python object, and nothing checks.

## The rule

Arrow is the wire format: the representation a frame is passed, stored, hashed,
and validated in. pandas exists at exactly two places, and both are places
authored code reads a frame:

| stage type | what the authored function receives | why pandas |
|---|---|---|
| `pandas_frame_function` | `transform(df)` — arbitrary pandas is permitted | the whole point of the type |
| `publish` | `fn(df, …, output_dir=…)` → returns a frame of artifact paths | writes a report from the whole frame |

The three other stage types that run authored code — `python_row_function`,
`filter_rows`, `starlark_row_function` — hand it a `Row` (`dict[str, Any]`), not
a frame. They are **not** pandas seams and must not become ones.

## Why pandas stays

`pandas_frame_function` and `publish` hand authored code a real DataFrame and
permit arbitrary pandas: `groupby`, `pivot`, `merge`, `.str` accessors. That code
is stored user data in every project. Changing what `transform(df)` receives
breaks all of it, and `PersistedModel.load` is a strict `model_validate` with
`extra="forbid"` — there is no lenient read to fall back on.

So the seam is a **permanent commitment**, not a migration step. A seam you
intend to remove eventually is one nobody designs properly: you end up carrying
the materialization *and* the ambiguity about whether it is load-bearing. The
price of saying permanent is one materialize/coerce round trip per authored-code
stage. That is the right price.

`app/runtime/stages/starlark_marshal.py` is the working model for the shape:
marshal out, run authored code, coerce the return against the declaration.

## The name

`python_frame_function` was renamed `pandas_frame_function` (alembic `0011`),
because pandas is what the type hands its authored function and naming that is
the point. The name is only exact once `publish` no longer takes a DataFrame;
until then it names one of two seams while the other stays unnamed, which is why
this page lists both.

Two things the rename did NOT do. A **run manifest** keeps the old literal: it
records what executed, and the type was called `python_frame_function` then, so
`StageType._missing_` maps the name on read rather than history being rewritten.
And a **stored spec** cannot be rescued that way — `Stage` is a discriminated
union, and pydantic resolves the tag before enum coercion — so specs are migrated
in both places they live: the document store (alembic `0011`) and a project's
on-disk `compiled/` working copy
(`python -m scripts.migrate_compiled_stage_files --apply`), which no revision
reaches.

The rename re-keyed `compute_definition_fingerprint` for every stage of this
type, because `type` feeds it. Accepted rather than worked around: the cost is
recompute of local Python, and no LLM spend or human decision is keyed this way.

## What enforces it

`tests/arch/test_pandas_seam_ratchet.py` is a burn-down ratchet on pandas types
in `app/` **signatures**. `_OWNERS` is the set of modules allowed to name one at
all; `_ALLOWLIST` carries every other module at its current count and may only
shrink. 122 signatures across 30 modules — 18 in `app/core/frames.py` (an
owner), 104 to burn down. It was 153 across 32 before arrow became the wire
format.

The ratchet governs signatures, which is what makes the coupling measurable. It
does not yet govern which modules may *materialize* a pandas frame; that rule
becomes assertable once the interior takes Arrow, and belongs with that change.

# The pandas seam

Where pandas is allowed to exist, and why it is allowed to exist there.

## Terms

- **Frame** — one table of rows moving between stages. Today it is a
  `pandas.DataFrame` everywhere; the direction of travel is a `pyarrow.Table`
  everywhere except the seam below.
- **Authored code** — Python a user of carbonpaper wrote and stored in their own
  project: the `function:` block of a `python_frame_function` or a `publish`
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
| `python_frame_function` | `transform(df)` — arbitrary pandas is permitted | the whole point of the type |
| `publish` | `fn(df, …, output_dir=…)` → returns a frame of artifact paths | writes a report from the whole frame |

The three other stage types that run authored code — `python_row_function`,
`filter_rows`, `starlark_row_function` — hand it a `Row` (`dict[str, Any]`), not
a frame. They are **not** pandas seams and must not become ones.

## Why pandas stays

`python_frame_function` and `publish` hand authored code a real DataFrame and
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

## What enforces it

`tests/arch/test_pandas_seam_ratchet.py` is a burn-down ratchet on pandas types
in `app/` **signatures**. `_OWNERS` is the set of modules allowed to name one at
all; `_ALLOWLIST` carries every other module at its current count and may only
shrink. 95 signatures across 25 modules to burn down, `app/core/frames.py`
excluded as the owner. It was 153 before arrow became the wire format.

What is left is not all wrong. Roughly a quarter of it is the presentation layer
(`app/web/*`, `app/services/frame_profile.py`), which renders HTML from parquet
and has no reason to hold an arrow table; and five signatures are pinned by the
stage cache, whose fingerprint addresses every entry already recorded. Those two
groups should become declared OWNERS rather than allowlist entries that never
reach zero. The rest — the sidecars, the row driver's internals, `input_data`'s
readers, and validation — are genuinely on the wire and should go.

The ratchet governs signatures, which is what makes the coupling measurable. It
does not yet govern which modules may *materialize* a pandas frame; that rule
becomes assertable once the interior takes Arrow, and belongs with that change.

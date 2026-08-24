# Branch analysis

A **branch** is one decision a run made that came out differently for different rows.

That is the whole idea. Everything below is about where branches come from, how a row carries
the ones it took, and what you can ask once every row carries them.

Nothing here is worked out by reading the workflow's code and predicting what it would do. Every
branch below is read back off what the run wrote while it executed.

## What one branch is

A branch has an id, the stage it happened at, and a label a person can read:

```
funded|dropped                    at `funded`, "dropped by the predicate"
read_money|transform/3:elif0      at `read_money`, "elif text.startswith('$')"
load_grants|west_export           at `(source)`, "loaded by west_export"
```

The id is `f"{stage_id}|{branch_id}"` and it is opaque — it is not a join key, not a group-by
column, and not a name anyone typed. The part after the `|` is only ever split back out to ask
one question: is this the `dropped` arm?

A branch carries a `BranchReason` (why the rows differ) and a `BranchRole` (what the branch did
to the rows that took it). Both are in `app/models/branch_analysis.py`.

## Where branches come from

Only **one** kind of branch is recorded while the run executes.

`app/core/branch_source.py` rewrites a stage's Starlark before it runs, opening every
`if` / `elif` / `else` / `try` / `except` with a call that reports itself. Each output row's list
of arms lands in `<stage>.branch.parquet` beside its frame. That is the `code` reason, and it is
the only one that costs anything to collect.

The other five are **worked out afterwards**, from the shape of the lineage sidecar. Lineage is
already written for every stage: `<stage>.lineage.parquet` says, for each output row, which input
rows it came from. `app/runtime/branch_analysis/run_branches.py` reads that graph and asks what
decision must have produced its shape:

| reason | what the lineage looks like | function |
|---|---|---|
| `load` | the stage has no inputs at all, so every row came off disk here | `_mark_loaded_here` |
| `union` | every row reaches exactly one of several inputs | `_mark_which_input` |
| `join` | there are two inputs and the second is reached, or is not | `_mark_join_misses` |
| `predicate` | one input every row reaches, and that input holds rows nothing reaches | `_mark_drops` |
| `aggregate` | several input rows carry `contribution` edges into one output row | `_find_groups` |

Two consequences worth holding on to.

**These five cost no extra recording and no re-run.** Any run that wrote lineage can be read this
way, including runs that finished before this code existed.

**They are inferences, and an inference can be wrong.** The `predicate` rule is the sharpest
example: it says *an input every output row reaches is the spine, so that input's own rows which
nothing reaches were dropped here*. A stage that removes rows will always look like that. So will
a stage that does something else and happens to leave rows unreferenced. Nothing the run asserted
checks the rule; it is a reading of the shape.

A dedupe is the case that shows this working. It writes one parent per surviving row and no edge
at all for a collapsed row — the same shape a filter writes — so the `predicate` rule gives it
kept and dropped arms with no code of its own. Only the wording differs, because a dedupe has
keys rather than a predicate (`_name_the_removal`).

## A path is every branch a row ever took

A branch is recorded at the stage that made it. A reader asks about a row twenty stages later.

So each row inherits its parents' branches and adds its own, walking the run in execution order
(`_carry_forward`). What a row ends up holding is a `BranchPath` — a tuple of branch ids, sorted
by stage position then source line so that **two rows that made the same decisions hold the same
tuple**. That equality is what the drawing is made of: rows on identical paths are one node.

Sorting is `_rank_branches`. Without it, two rows that took the same decisions in the same order
could still hold their branches in different orders and never compare equal.

## What a branch did to its rows

`BranchRole` has three values, and they answer one question: what happened to the rows that took
this branch?

| role | meaning |
|---|---|
| `removes` | those rows were taken out of the frame — a filter's drop, a dedupe's collapse |
| `excludes` | those rows are still in the frame, but in a different group of an aggregate |
| `keeps` | neither; the rows carried on |

`keeps` does **not** mean the rows reached any particular figure. It means this branch did not
remove them. In the test fixture, `size_band` removes nothing, so all of its arms are `keeps` —
and the row that took `if amount == 0` is still dropped two stages later at `funded`.

## Asking which rows produced a figure

`app/services/scope.py` answers a citation — a stage, a row and a column, e.g.
`people_figures` row 0, column `deaths_gb_review_period`.

It starts at that cell and asks whether the row is a group. A row is a group when the lineage
records several rows feeding it by `contribution` edges. If it is, the row is replaced by the
rows that fed it and the question is asked again, until nothing is a group (`_expand`). What
comes back is a `RowSet`: a stage, and ordinals into that stage's frame.

Two things it deliberately does not do.

**It does not follow a stage that recorded nothing.** If the lineage is absent, the walk stops
there rather than assuming output row *i* is input row *i*.

**It refuses a figure whose rows do not sit at one grain.** If expanding a cell bottoms out in
two different stages, there is no single frame the rows live in, and it raises
`UnresolvableFigure` rather than presenting a mixture.

`measure_frame_scale` is the same walk with a different question: for each stage, how many of
*its* rows this figure came through. That is a count per stage, never a shape drawn to scale —
40 rows beside 45,061 cannot be drawn as two ribbons without lying about one of them.

## Asking which rows a branch cut

The inverse, in `app/runtime/branch_analysis/rows_behind_a_branch.py`. Given a branch id, which
rows took it — and, the part that is easy to get wrong, **which frame those rows are in**.

For most branches the rows are in the stage's own output frame, and holding the branch is enough
to select them. For the `dropped` arm it is the opposite: those rows are not in the output at all,
by definition. They are in the stage's *input* frame, and they are found as the rows of that
input which no output row reaches (`_find_lost_rows`).

An `aggregate` branch is a third case: its rows are the contributors that fed one output row, and
they live in the aggregate's input frame too (`_find_group_members`).

## Where the code lives

| module | holds |
|---|---|
| `app/models/branch_analysis.py` | `BranchFact`, `BranchReason`, `BranchRole`, `BranchPath`, `RowSet`, `FrameScale` |
| `app/core/branch_source.py` | finding and instrumenting a stage's branches, and locating the line one tests on |
| `app/runtime/branch_analysis/run_branches.py` | reading the sidecars, working out the other five reasons, carrying paths forward |
| `app/runtime/branch_analysis/stage_code.py` | the stage source a branch decided in |
| `app/runtime/branch_analysis/rows_behind_a_branch.py` | which rows took one branch, and in which frame |
| `app/services/scope.py` | answering a citation, and measuring frame scale |
| `tests/scope_fixture.py` | twelve grants and fourteen stages, hitting every reason on purpose |

`app/runtime/branch_analysis` reads a finished run rather than executing one, which is not the
runner's job. It sits inside `app.runtime` for now because that is where the sidecar readers are;
see [issue 834](https://github.com/surveit/carbonpaper/issues/834) for pulling both out.

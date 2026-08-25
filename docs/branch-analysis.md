# Branch analysis

A **branch** is one decision a run made that came out differently for different rows.

That is the whole idea. Everything below is about where branches come from, how a row carries
the ones it took, and what you can ask once every row carries them.

Nothing here is worked out by reading the workflow's code and predicting what it would do. Every
branch below is read back off what the run wrote while it executed.

## What one branch is

A branch has an id, the stage it happened at, and a label a person can read:

```
funded|removed                    at `funded`, "dropped by the predicate"
read_money|transform/3:elif0      at `read_money`, "elif text.startswith('$')"
load_grants|loaded                at `load_grants`, "loaded by load_grants"
mark_groups|transform/5:choice1:else   at `mark_groups`, "else"
```

The id is opaque. Nothing reads anything back out of it — a `BranchOption` carries every fact a
caller needs as a field, including `rows_live_in_stage_id`, the frame its rows are rows of.

A branch carries a `BranchReason` (why the rows differ) and a `BranchRole` (what the branch did
to the rows that took it). Both are in `app/models/branch_analysis.py`.

## How many options a stage has

Most stages offer a fixed number, readable off the workflow before anything runs: an `if`/`elif`
chain has one per arm, so does a conditional expression, a filter has kept and dropped, a join
has matched and missed per input, a union has one per input, a load has one.

A `group_by` does not. It offers **one option per group**, and how many groups there are is decided
by the data: group ten grants by the portfolio their agency belongs to and you get however many
distinct portfolios those ten rows carry. So the catalogue is per-run, and
`BranchOption.merged_into_row_ordinal` is the part that varies.

That is not a wart. "Which group did you go into" is the same shape of question as "which arm did
you take"; it just has more answers, and they are not known until the rows arrive.

It does mean a drawing cannot offer one option per group. See "Aliasing a merge" below.

## Where branches come from

Only **one** kind of branch is recorded while the run executes.

`app/core/branch_source.py` rewrites a stage's Starlark before it runs, opening every
`if` / `elif` / `else` / `try` / `except` with a call that reports itself. Each output row's list
of arms lands in `<stage>.branch.parquet` beside its frame. That is the `code` reason, and it is
the only one that costs anything to collect.

A conditional expression — `1 if in_window else 0` — is recorded too, and it is the case a
statement rewrite cannot reach: an arm is a value, with no suite to put a call above. So each arm
is wrapped instead, `(record_branch("…") or (1))`, which yields the arm itself because the
recorder returns `None`. A `ChoiceBranch` carries the arm's end position for that wrap, and its
own label (`if in_window`, `else`), since no line opens it for `read_branch_test` to read.

Two places a conditional expression is deliberately **not** recorded. Inside a comprehension or
a lambda it runs per element and per call, so "which arm did this row take" has no one answer.
And a `def`'s own decorators and defaults run where the `def` is written, not once per row.

The other five are **worked out afterwards**, from the shape of the lineage sidecar. Lineage is
already written for every stage: `<stage>.lineage.parquet` says, for each output row, which input
rows it came from. `app/runtime/branch_analysis/run_branches.py` reads that graph and asks what
decision must have produced its shape:

| reason | what the lineage looks like | function |
|---|---|---|
| `load` | the stage has no inputs at all, so every row came off disk here | `_read_the_load` |
| `union` | every row reaches exactly one of several inputs | `_read_which_input` |
| `join` | there are two inputs and the second is reached, or is not | `_read_join_misses` |
| `predicate` | one input every row reaches, and that input holds rows nothing reaches | `_read_removals` |
| `merge` | several input rows carry merge edges into one output row | `enumerate_merges` |

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
(`build_branch_path_per_row`). What a row ends up holding is a `BranchPath` — a tuple of branch ids, sorted
by stage position then source line so that **two rows that made the same decisions hold the same
tuple**. That equality is what the drawing is made of: rows on identical paths are one node.

Sorting is `_rank_branches`. Without it, two rows that took the same decisions in the same order
could still hold their branches in different orders and never compare equal.

## What a branch did to its rows

`BranchRole` has two values, and they answer one question: what happened to the rows that took
this branch?

| role | meaning |
|---|---|
| `removes` | those rows were taken out of the frame — a filter's drop, a dedupe's collapse |
| `keeps` | the rows carried on |

There is deliberately no third value for "in a different group". Whether a merge branch included
or excluded a row depends on **which output row you asked about**, and the branch does not know
the question. `app/services/scope.py` answers that, where the citation is in hand.

`keeps` does **not** mean the rows reached any particular figure. It means this branch did not
remove them. In the test fixture, `size_band` removes nothing, so all of its arms are `keeps` —
and the row that took `if amount == 0` is still dropped two stages later at `funded`.

## Asking which rows produced a figure

`app/services/scope.py` answers a citation — a stage, a row and a column, e.g.
`grant_totals` row 0, column `total_amount`.

It starts at that cell and asks whether the row was merged from several. It was, when the lineage
records several rows feeding it by merge edges. If so, the row is replaced by the rows that fed
it and the question is asked again, until nothing was merged (`_expand`). What comes back is a
`RowSet`: a stage, and ordinals into that stage's frame.

Two things it deliberately does not do.

**Absent lineage is a declaration, not a gap.** A stage whose type preserves its rows writes no
lineage, because output row *i* IS input row *i* by contract — `is_grain_and_order_preserving()`
says which types those are. Twelve of the twenty-three stages in a real workflow are in that set.
Any other type that writes none raises `MissingLineage` rather than being guessed at.

**Every expansion bottoms out in one frame.** Only an `aggregate` stage writes merge edges, and
it names one input stage on every row it emits, so the rows a cell expands to always sit at one
grain. The one aggregate row that no input row fed — a whole-frame aggregate over an empty frame,
which is one row by construction — is its own row set, and the walk stops there.

**A frame can be read by more than one merge, and only one is on your route.** Group the same
grants by portfolio and by region, and every row holds a branch from both. Asked about a portfolio
total, the region branch splits rows that the question does not distinguish, so `RowSet.regrained_at`
records the merges the walk actually came down through and the rest are dropped from the path.

`regrained_at` records every re-graining the walk came down through. A drawing resolves only the
nearest of them; see "Aliasing a merge".

`measure_frame_scale` is the same walk with a different question: for each stage, how many of
*its* rows this figure came through. That is a count per stage, never a shape drawn to scale —
40 rows beside 45,061 cannot be drawn as two ribbons without lying about one of them.

## Asking which rows a branch cut

The inverse, in `app/runtime/branch_analysis/rows_behind_a_branch.py`. Given a branch id, which
rows took it — and, the part that is easy to get wrong, **which frame those rows are in**.

`BranchOption.rows_live_in_stage_id` answers it, so no caller works it out. For most branches it
is the stage's own output frame. For a `removes` branch it is the opposite: those rows are not in
the output at all, by definition, so it names the *input* frame and they are the rows of it no
output row reaches (`_find_removed_rows`). A `merge` branch names the merged stage's input frame
too, and its rows are the ones merged into one output row (`_find_merged_rows`).

## Aliasing a merge

A code branch has as many options as the code has arms. A merge has as many as the data has
groups — 6,132 at one `group_by` in a run of 37,403 rows. So a merge stage is drawn as **one
node** standing in for every group it made. That standing-in is called **aliasing**, and it is
a view choice only: the groups are in the lineage either way.

One merge is resolved by default — the re-graining nearest the cited cell, which is what the
figure is composed of (`find_nearest_merge`). Every merge below it is aliased, and the drawing's
node count is then the workflow's branches rather than the data's groups. On a real figure over
35 country-years, that is 2 nodes instead of 35.

A reader expands one with `?expand=<stage_id>`, repeatable. An expanded merge is resolved like
the nearest, so the drawing splits into a node per group those rows went into. The nearest merge
is always resolved and so is never offered for folding.

**An arm of a resolved merge is named by its group keys** — `income_group = High income ·
year_int = 2024`, read off the stage's own frame (`name_the_groups`), never the ordinal it
landed on.

**The merge stage is still drawn while aliased.** Its column holds one node saying how many of
its groups this figure came through. A merge that vanished from the drawing would hide the
re-graining, which is the thing a reader most needs to see.

A filter is untouched by any of this. Its `predicate` branch has its own id and its own stub,
so aliasing a merge never hides what a stage removed.

**Aliasing is a view choice, and only the view knows about it.** `group_rows_by_path` in
`app.runtime` takes `told_apart_by` — the branch ids these rows may be told apart by — and
knows nothing about merges or aliases. `find_branches_that_tell_rows_apart` composes the two
facts that decide it: on the route, and, for a merge, at a stage the reader expanded. An empty
set is a real answer, so it is a required argument rather than a defaulted one.

## Where the code lives

| module | holds |
|---|---|
| `app/models/branch_analysis.py` | `BranchOption`, `BranchReason`, `BranchRole`, `BranchPath`, `RowSet`, `FrameScale` |
| `app/core/branch_source.py` | finding and instrumenting a stage's branches, and locating the line one tests on |
| `app/runtime/branch_analysis/run_branches.py` | reading the sidecars, working out the other five reasons, building a path per row |
| `app/runtime/branch_analysis/stage_code.py` | the stage source a branch decided in |
| `app/runtime/branch_analysis/rows_behind_a_branch.py` | which rows took one branch, and in which frame |
| `app/services/scope.py` | answering a citation, measuring frame scale, finding the nearest merge |
| `app/web/merge_alias.py` | `AliasedMerge`: which merges a drawing resolves, and what names their groups |
| `tests/scope_fixture.py` | twelve grants and fourteen stages, hitting every reason on purpose |

`app/runtime/branch_analysis` reads a finished run rather than executing one, which is not the
runner's job. It sits inside `app.runtime` for now because that is where the sidecar readers are;
see [issue 834](https://github.com/surveit/carbonpaper/issues/834) for pulling both out.

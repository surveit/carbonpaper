# The scope map

A claim cites a cell — `StageOutputCellCitation(stage_id, row_ordinal, column)`, say
`paid_totals.total_income_usd` at row 0, worth 4,461,000. The scope map answers one question about
it: **which rows produced this number, and what told them apart from the rows that did not.**

Every distinction it draws was recorded while the run executed — a branch the code took, a filter's
verdict, a join hitting or missing, which group an aggregate put a row in. None of it is inferred
by reading the workflow's code afterwards.

The models are in `app/models/scope_map.py`; the builder is `app/runtime/scope.py`, which is
self-contained on a run directory the way `app/runtime/trace.py` is.

## A branch is a label over lineage

`app.runtime.lineage` records, for each output row, which input rows it came from and by which
`EdgeKind`. A **branch** is one way of telling those edges apart. Every origin is the same
mechanism:

| origin | discriminates on | arity |
|---|---|---|
| `load` | a stage with no inputs read the row off disk | 1 |
| `union` | which input of a union the row arrived on | one per input |
| `lookup` | the reference input matched, or missed | 2 |
| `predicate` | the filter kept the row, or dropped it | 2 |
| `code` | the `if`/`elif`/`else`/`try` arm in the branch sidecar | 2 or more |
| `aggregate` | which group of the aggregate the row fed | one per output row |

Only `code` comes from the branch sidecar the instrumenter writes. The other five are read back off
the lineage sidecar, so they cost no extra recording and no re-run.

The two names that read oddly are forced: a `StrEnum` member may not shadow a `str` method, so the
join origin is `lookup` and the partition origin is `union`.

An `aggregate` branch is recorded on the **contributor** rows and keyed by the aggregate's stage —
the same shape a filter's `dropped` arm already uses, where the arm labels rows of the input frame.
Without it, selecting rows by their branches over-collects badly: a `group_by` partitions on a data
value and no `if` ever tests it, so rows in different groups routinely sit on identical paths.

## `dedupe` and `aggregate` write the same edges and mean opposite things

`EdgeKind` has two members, `direct` and `contribution`. `handle_dedupe` writes the surviving row as
a `direct` edge and every discarded duplicate as a `contribution` edge — the same kind an aggregate
uses for a genuine contributor. Nothing in the edge separates them.

The **stage type** is the declaration:

- `dedupe` — duplicates are errors carrying no information. Read it as a filter: the survivor is the
  row, the losers are drops, and they belong in a cut rather than in the population.
- `aggregate` — an entity resolution. Every contributor fed the output row and is in the population.

A builder that filters for `kind == contribution` without asking the stage type descends a dedupe
into the *discarded* row and never reaches the survivor.

## What a row's path means at an aggregate

At a row-preserving stage a path is what that row did. An aggregate's output row is reached by many
paths at once, so its path reads as *descends from* rather than *came from*, and it can hold two
arms of one decision — a deduped filing that appeared in both quarterly exports descends from both.

That is not a defect. A node is identified by its whole branch set, so such a row lands in its own
third node ("came from Q1 + came from Q2") and every row is still in exactly one node. What does
break is counting **per branch** instead of **per node**: a row holding two arms is counted under
each, and the arms of one split then total more than the rows they split.

## Three things an arm can mean, and only two are drawn

`BranchRole` separates them:

- `removes` — the rows were taken out of the frame. A filter's `dropped`, a dedupe's `duplicate`.
- `excludes` — the rows are still in the frame, but in another group of an aggregate this figure
  came through.
- `arm` — nothing happened to them here. They carried on, and they left the figure somewhere else.

Only the first two are worth drawing beside a stage. Drawing an untaken `arm` as a loss says a row
function removed a row, which no row function does: the row that took the other arm is still
downstream, and it left at some filter further along.

To find out *why* a set of rows is missing, redraw for those rows. The reason is usually upstream of
the stage that removed them — Venezuela's 44,963 dropped filings turn out to share one path, and it
ends at `decide_inclusion`'s `else`, two stages above the filter that dropped them.

## Which rows, not how much

The map always answers which rows reached the figure. Their values total to it only when the drawn
frame is the one the formula read **and** the formula adds — `ContributingRowSet.adds_up`. A sum of
per-group means resolves to rows in a frame that does not even contain the value column; the
population is right and the total would be meaningless. Where `adds_up` is false, show the count and
say why, rather than printing a number that contradicts the figure above it.

## Counts over every row, cells over a sample

A cut can be most of the corpus. Venezuela's largest is 44,963 rows, and they fall on **two** paths,
so the whole drawing ships as one count per path — 0.3 kB. Their cells would be 44.8 MB, and a
browser will not lay out 45,000 rows anyway. So `CutRows` carries exact counts and a sample of
cells, and a reader is told which is which.

`FrameScale` is the same discipline applied to the figure itself: a stage's frame size beside the
count the figure descends from, so a map over 40 rows says that the frame it came through held
45,061. A count, never a ribbon — drawing the two to scale at once is the scale-mixing that makes a
picture lie.

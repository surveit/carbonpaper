# The retired `where` on an aggregation

An `aggregate` stage's aggregation may carry `where: "<predicate>"`. The predicate cuts
which input rows feed that ONE output column, leaving every other column of the same
output row computed over a different set of rows.

The field is **gated, not deleted**: it is withheld from the JSON schema the stage-writing
tools hand a model (`SkipJsonSchema` on `AggregationOp.where`), and
`app.services.stage_edit` refuses any write carrying one. A stored `workflow_version` is
immutable, so the field still parses, still validates its predicate against the input
columns, and still executes in `app.runtime.stages.aggregate` — a version written before
the gate runs today exactly as it ran when it was signed off.

## Why it is gated

**It is the only row-cut that is not a stage.** Every other cut in Carbon Paper is a
stage of its own — it appears in the graph, it has a row count, a reviewer can open it,
a stage test can pin it, and a trace names it with its predicate. A `where` does the same
job invisibly: nothing in the workflow view says a cut happened.

**A row carrying one has no single population.** In the Venezuela lobbying workflow,
`paid_totals` row 0 was 40 filings for four of its columns and 2 filings for the fifth.
"Which rows produced this figure" then cannot be answered from the row alone — it needs a
column too, and answers differently per column. That is why `RowParent.columns` exists at
all: per-column contributor attribution is what a `where` forces.

## What to write instead

The cut becomes a grouping. An aggregation of the form

```json
{"output_column": "exception_rows", "formula": "count", "where": "basis == 'exception'"}
```

is one cell of a pivot. Grouping on the column the predicate tests emits the whole pivot
as rows instead:

```json
{"group_by": ["basis"], "aggregations": [{"output_column": "rows", "formula": "count"}]}
```

Every category comes out, each row has one population, and a reader who wants the
`exception` figure reads the `exception` row. Where the published grain must stay one row
per organisation, a sibling aggregate at the finer grain — or a `list` of the category
column — carries the same fact without splitting the row's population.

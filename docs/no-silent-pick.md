# Collapsing rows without picking one

Two places in Carbon Paper turn several rows into one: an `aggregate` grouping, and a
`dedupe`. Neither may choose between rows that disagree without saying so.

## `aggregate`: `first` and `first_including_null` are retired

Both take a value off whichever row sorted first. Where the group's rows disagree, one
value is published and the rest vanish with nothing recording that the sources differed.

Measured on the local store when the gate was added: 40 such aggregations across 5 stages
in 3 projects. In `venezuela_lobbying_q1_q2_2026`, filing `620bb255` is reported in both
quarterly LDA exports, spelled `BALLARD PARTNERS, LLC` in one and `BALLARD PARTNERS` in
the other; `first` kept the first. The same happens in
`venezuela_lda_lobbying`'s `spend_by_firm`, which is a genuine reduction rather than a
disguised dedupe — so the defect is the formula, not the shape of the stage.

Both members stay on `AggFormula`, so a stored `workflow_version` still parses and still
runs. They are withheld from the JSON schema the stage-writing tools advertise
(`WithJsonSchema` on `AggregationOp.formula`) and refused on write by
`app.services.stage_edit`.

**Write instead:**

| The group… | Use |
|---|---|
| agrees on the column | `only` — carries the agreed value, fails naming the values where they differ |
| genuinely differs | `list` — every value, in the order the rows arrived |
| is duplicate rows, not a summary | a `dedupe` stage in front |

`only` treats NULL as absence, not as a second value: a column that is `'BALLARD PARTNERS'`
on one row and null on another carries `'BALLARD PARTNERS'`.

## `dedupe`: `keep: agree`

`keep: agree` chooses no survivor. It requires the rows sharing a key to be identical on
every column outside `keys`, and fails naming the column and the differing values where
they are not. Unlike `only`, a null counts as a value: the survivor IS one of the input
rows, so taking the row that happens to hold the null is still a choice, and a dedupe
cannot coalesce — its output rows are input rows unchanged.

`keep: first`, `keep: highest` and `keep: lowest` remain. They state a rule a reviewer can
read, and a run that must pick one of two genuinely different rows needs one.

## What a refusal looks like

```
stage 'one_row_per_filing': the 2 rows with filing_uuid='f7eac1b9-...' — 2 different
values of `registrant`: 'BALLARD PARTNERS, LLC', 'BALLARD PARTNERS'. Collapsing them
publishes one and drops the rest with nothing recording that the sources disagreed:
settle it upstream, or carry the column with formula `list`.
```

Both refusals are raised by `app/runtime/stages/agreement.py`, which the aggregate handler
and the dedupe handler share.

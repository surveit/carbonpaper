# The figure card

A permalink for **one published figure**, written for a reader who has never heard of this
app — the page you paste into Slack, a Bluesky post or an article footnote.

`GET /figure/{project_id}/{run_id}/{stage_id}/{row}/{column}`

Code: `app/web/routers/figure_card.py` (its own router — `app.web.routers.runs` is at the
import fan-out ceiling), `app/web/figure_card.py`, `app/templates/figure_card.html`,
`app/static/figure-card.css`.

## What counts as a figure

A **published figure** is a value a `publish` stage claimed with
`citation_provider.cite_value(...)`. The provider checked the cell held that value before
recording it (`app/runtime/citations.py`), so a `CitedValue` is a number and the cell it
came out of, never a number typeset in publish. A cell no publish stage cited has no
label, no claim and no card: the route 404s and says so.

The card prints `CitedValue.value` — the cell as the run stored it. It does not round it,
group its digits or give it a unit; the label the publish stage wrote is what says what
the number is.

## The receipt

Read off the scope map (`app/web/scope_view.py` → `app/web/scope_payload.py`), which
already answers "which rows produced this cell". Nothing here walks it a second time.

| the card says | read from |
|---|---|
| rows it was counted from | `ScopeMap.covers` — the row set past every merge on the route |
| the widest frame it came through | the largest `rows_count` in `ScopeMap.scale` |
| rows the run took out | `find_cuts_to_offer`, each cut's `total` and its branch label |
| rows nothing fed | `RowSet.fed_by_no_rows` |
| who did each step | `ScopeMap.stages` split by `TYPE_CLASS` |
| reference tables | `ScopeMap.lookup_tables` — looked things up in, never counted |

**Who did each step** is three claims, not one: `llm` is a model's judgement, `human` is a
person's decision, and every other class is code that runs the same way each time. A
workflow with no review step on the route says nobody signed the figure off; that is the
answer, not a gap.

## When the run recorded no lineage

The walk raises `MissingLineage` where a stage owed a lineage sidecar and wrote none — a
`python_frame_function` in the middle of a workflow is the common case. The card then
carries `NoReceipt`, states the reason it was given, and shows **no counts at all**. A
zero would read as "nothing was dropped"; the truth is that this run cannot say.

## The link preview

`og:title`, `og:description`, `og:url` and `twitter:card` are in the page head, and the
description is counted off the receipt at render time.

**There is no `og:image`.** Drawing one would need an image library this project does not
carry, and no stock picture says anything about the figure. Text-only previews unfurl in
Slack, Bluesky and Mastodon.

## The way in

`GET /project/{p}/runs/{run_id}/figures` lists every value that run's publish stages
cited, each linking its own card. The run page's **Run outputs** section links it.

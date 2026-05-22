# Rethink: how the prototype should work given what it's for

Written 2026-05-21 after applying the LobbyMap-shaped pipeline to a new
domain (US Congress + lobbying), running it end-to-end on a real slice, and
reviewing the experience as a journalist would.

This is a recommendation memo, not a plan. It says **what to change and why**
in roughly priority order, with enough detail that the change is concrete but
not so much that it pre-decides the implementation. References to specific
files are pointers, not the prescription.

## Core diagnosis

**The prototype works very well as a methodology-maintenance tool, and
poorly as a journalism-discovery tool.** Everything visible from the index
page out — DAG, stage detail panels, prompt templates, validation reports,
queue cards — is built for the person who *authored* the methodology. The
artifact a journalist actually wants to file from (the per-entity profile
pages) sits at the very end of one click chain, contains no cross-cutting
affordances, and reads as a static dump.

This isn't a UI polish issue. It's structural: the pipeline assumes the
unit of investigation is "the entity," and the data model + publish step
encode that. But the journalism questions a reporter brings to data like
this — "who's the outlier on issue X?", "which lobbyist–rep pair has the
biggest rhetoric–money gap?", "what changed week-to-week?" — are
cross-entity, not per-entity.

Five recommendations follow. The first three are about reorienting the
output. The last two are about what the prototype assumes about its data.

## Recommendation 1: Invert the front door

Right now the user journey is:

> index → methodology → DAG → run → stage panel → (eventually) artifact

A journalist visiting for the first time has to traverse four
maintenance-y screens before they see a story-shaped output. By the time
they get to a member profile they've already lost interest in the
methodology.

The front door for a finished run should be a **cross-cutting view**: a
sortable table of all entities × all queries, filterable by party, state,
chamber, cell score range, evidence count, and lobbying spend on the
issue. The current per-entity profile is one click in from that table,
not the destination.

Concretely:

- Add a `views/` stage type alongside `publish`. A view is a JSON or
  HTML render that takes the entire output table and emits a queryable
  artifact (sortable table, leaderboard, scatter plot, change diff).
- The default published artifact should be `views/scoreboard.html` —
  the cross-cutting table — not `profiles/<entity>.html`.
- Profile pages stay, but they're reached from a cell click in the
  scoreboard.

The pipeline-maintenance UI (DAG, queue, stage panels) stays where it is,
but is reached via a "behind this run" link, not the main path. Two
audiences, two doors. Same data.

## Recommendation 2: Stop forcing every output through a numeric score

The `cell_aggregation` stage produces one number per (entity, query).
That number is supposed to summarize "where does this entity stand."
After running on real data, the number lies as often as it informs:

- Durbin, with 14 evidence pieces almost all supportive of extending the
  ACA tax credits, scored +0.21. Indistinguishable from "neutral."
- A member with one strong-support quote and one mock-LLM stance-flip
  scored 0.
- A member with zero evidence on a query — different from "neutral" —
  also shows as no cell (which is correct), but the UI treats absent
  cells and zero cells visually identically.

The single-number-per-cell shape is borrowed from LobbyMap, where
objective benchmarks and large evidence volumes per company per query
let weighted means stabilize. Neither holds for political speech in a
one-month slice. **For most journalism cases the right summary is a
small structured record, not a number:**

```json
{
  "supportive_count": 9,
  "opposed_count": 2,
  "mixed_count": 3,
  "directional_label": "strongly supportive (with caveats)",
  "top_quote_id": "PR_D000563_2026-01-21_47291::Q1::0",
  "stance_volatility": "low"
}
```

Make `cell_score` one optional summary view of that structured record,
not the canonical aggregation. The scoreboard from Recommendation 1
shows directional_label + supportive_count as primary, with the score
as secondary.

## Recommendation 3: Document-to-entity attribution must be a first-class stage type

Half the value in lobbying data is in *documents that don't carry
per-entity stance.* Lobbying filings are by-filer, by-client, by-issue;
they're not member statements. The current prototype has no way to model
"this is context, not evidence." I forked the pipeline into two parallel
streams that meet at publish — workable, but the seams show.

Propose a new stage type **`context_join`** that takes two inputs:
- evidence-level data with entity_id + topic
- context-level data with topic but no entity_id

It outputs entity-rows enriched with per-topic context (counts, top
references, top filers, top clients). The cell_aggregation stage
consumes both the evidence aggregate and the context-join result. This
makes "lobbying-on-issue alongside member-stance-on-issue" a structural
relationship the system understands, not a side channel.

This also makes the boilerplate-mistake the UX agent flagged
(`profiles/D000563.html` showing identical top clients across all five
queries) impossible: the context_join is per (entity, query), so the
template gets per-pair top clients.

## Recommendation 4: Acknowledge that some methodologies have no neutral benchmark

LobbyMap's scoring works because IPCC supplies an objective benchmark
that everyone agrees represents "the science." For most domestic
policy issues — ACA tax credits, Medicaid funding, drug imports —
there is no such authority. The score axis is a *framing choice*.

The prototype currently has no way to surface this distinction. I tagged
the CongressWatch benchmarks with `kind: stance_axis` to mark them as
stipulated rather than expert-derived, but no part of the UI shows that
to a reader. A journalist looking at "Durbin scored +0.2 on ACA tax
credits" doesn't know whether +2 represents IPCC consensus or one
person's framing.

Three small changes would close this:

- Add a `framing` field to the methodology root: `{authoritative,
  stipulated, contested}`. Display prominently on every artifact.
- Show the benchmark's `left_pole` / `right_pole` text on the
  profile page next to the score — not just the rubric tier label.
- In the disclaimer block on each artifact, generate the framing
  language from the methodology field, not from copy-paste.

This is small to implement and addresses a real legal/editorial risk:
publishing a "score" implies measurement against a baseline. The reader
needs to know what that baseline is.

## Recommendation 5: Mock LLM should fail loudly, not silently

The mock LLM is necessary for prototype demos without API keys, and the
current implementation is well-engineered. But its outputs are not
clearly marked as synthetic. A user reading the run detail page sees
`12 ms · 765 rows`, opens the evidence_extraction panel, sees a real
prompt template with real model name `claude-sonnet-4-6`, and reads
rationales like "matched pattern for Q1_aca_premium_credits". Those
rationales look LLM-shaped. They aren't.

The UX agent specifically called this out: "I'd lose trust here." When
real numbers are mixed with mock numbers and there's no way to tell, the
prototype's credibility goes to zero.

Concrete changes:

- Add a `mock: true` field to manifest records for stages whose handler
  used a mock. Every UI element that shows that stage's output gets a
  clear "🤖 mock data — not from a real model" badge.
- Mock LLM output should preserve a `_mock_reason` column showing the
  exact keyword pattern that matched. Surface it on hover.
- Refuse to publish a real artifact (HTML profiles) if any upstream
  stage was mock. Allow a `--demo` override that bakes a watermark into
  the page.

This matters more than the rest because it determines whether the
prototype can be shown to a journalist who didn't build it without
misleading them.

---

## What NOT to change

A short list of things the prototype got right that should be preserved
through any refactor:

- **Typed schema + validation between stages.** This caught real-data
  quirks immediately (duplicate filing_ids, null filer_org) on the
  CongressWatch first run. Worth its weight.
- **Halt-on-review + content-hashed decisions.** This worked perfectly
  end-to-end on a halted run, and the content-hash policy means decisions
  survive LLM non-determinism. Production-grade already.
- **Methodology as YAML + prose + code + data, sitting in `examples/<name>/`.**
  Forking from LobbyMap to CongressWatch by copy-paste and edit worked
  with no surprises. The methodology-as-directory pattern is right.
- **The DAG view, with run status colors and click-to-load detail.** A
  journalist reviewing a methodology's *structure* (not their daily
  reporting view) finds this immediately useful — it's the right
  affordance for the right audience.

---

## Suggested order if you act on this

Ranked by leverage:

1. Mark mock data clearly (Recommendation 5). Highest blast radius, two
   afternoons of work. Without this everything else built on top can
   mislead.
2. Add the scoreboard view (Recommendation 1, partial). The cross-cutting
   table by itself addresses 80% of the UX agent's findings. One full
   day of work.
3. Per-pair context joins (Recommendation 3). Fixes the "identical top
   clients across queries" bug and gives lobbying-vs-stance the
   structural status it needs. Two days.
4. Multi-shape cell summary (Recommendation 2). Bigger refactor —
   touches handlers, schema, publish templates. A week including the
   per-entity profile redesign.
5. Framing-mode tagging (Recommendation 4). Easy but cosmetic;
   defer until at least one downstream consumer needs it.

The prototype is on the right track. The maintenance / engineering layer is
already a real asset. What it needs next is to grow a journalism layer that
treats the data as a story to be found, not a pipeline to be inspected.

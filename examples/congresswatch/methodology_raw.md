# CongressWatch Methodology

A methodology for surfacing public stances by US members of Congress on hot
policy issues, cross-referenced with lobbying activity on those same issues.
Modeled after InfluenceMap's LobbyMap but applied to political actors rather
than corporations.

The journalistic question this is designed to answer:

> Which members of Congress are publicly advocating positions consistent with
> heavily-lobbied private interests, and which are pushing back? Where are
> the most striking alignments between rhetoric and lobbying spend?

## §1 Universe selection

The tracked entities are **individual members of the US House and Senate** who
were active during the time slice. A member is considered active in a period
if at least one press release from them appears in that period. The universe
is not pre-curated; it is derived from the press corpus.

Member identity is keyed on `bioguide_id`, the official Congress.gov
identifier. For each member we record: name, chamber, state, party.

Caucuses, committees, and party leadership roles are out of scope for v1.

## §2 Document sources

Two document classes feed the pipeline:

- **Press releases**: scraped from member.house.gov and senate.gov subdomains.
  These are official, member-attributed statements — the most direct expression
  of a member's public stance. Source class: `press_release`.

- **Lobbying disclosures**: quarterly LDA filings (House XML + Senate JSON).
  These do NOT carry a member's stance; they record what *lobbyists* did,
  filtered to filings that touched policy issues the member is publicly
  discussing. Source class: `lobbying_filing`. These are background context,
  not direct evidence of member stance.

In v1 we extract stance evidence ONLY from press releases. Lobbying filings
are used at the issue level (which clients filed, what they spent) and joined
to per-member scores in the final aggregation, not used as per-member
evidence.

## §3 Policy queries

A policy query is a discrete, topical position that a member might take. For
each query we record an ID, a short title, and a one-line description of the
position spectrum. For the healthcare slice the catalog is:

- **Q1 aca_premium_credits** — the enhanced ACA premium tax credits that
  expired 2026-01-01. Stance spectrum: support extension ↔ oppose extension.
- **Q2 medicare_drug_pricing** — Medicare negotiating prescription drug
  prices. Spectrum: expand IRA drug negotiation ↔ repeal it.
- **Q3 medicaid_funding** — federal Medicaid funding and work requirements.
  Spectrum: defend funding / oppose work requirements ↔ cut funding / require
  work.
- **Q4 ppi_drug_imports** — drug importation from Canada and other
  countries. Spectrum: expand pathways ↔ block as safety risk.
- **Q5 insurer_regulation** — health insurer denials, network adequacy,
  prior auth reform. Spectrum: tighten regulation ↔ market solutions only.

The query catalog is a CSV input to the pipeline, not hard-coded. Adding a
new topic means adding a row.

## §4 Evidence extraction

For each press release we ask the model: which (if any) of the policy queries
does this text take a stance on, and what is the stance?

Output per evidence piece:
- `query_id` — which policy
- `quote` — verbatim text from the document supporting the assertion
- `stance_summary` — "supports / opposes / mixed / unclear"
- `confidence` — 0..1 from the model
- `evidence_id` — synthesized from doc + query

A single press release may yield zero, one, or multiple evidence pieces. A
release that mentions ACA only in passing produces no evidence; one that
specifically advocates for or against extension produces one piece per
distinct stance taken.

## §5 Benchmark scoring

Each evidence piece is paired with the **policy query's stance scale** and
scored to integer in [-2, +2]:

- +2: strongly endorses the "active reform / pro-extension / pro-regulation"
  direction
- +1: weakly supports that direction
- 0: mixed or unclear
- −1: weakly opposes
- −2: strongly opposes

Unlike LobbyMap (which uses IPCC as an objective benchmark), there is **no
neutral expert authority** for most domestic policy questions. The score
direction is a *stipulated framing*, chosen per query in §3 and noted on the
query catalog. The system is not claiming truth about policy correctness — it
is consistently locating members on a spectrum so the reader can see who
clusters where.

The scoring prompt explicitly tells the model: "The score reflects alignment
with the LEFT pole of the stance spectrum as defined in the query. It is NOT
an alignment with a normative answer."

## §6 Human review queue

Any evidence with absolute score = 2 OR confidence < 0.6 is routed to a
review queue. Reviewers either confirm the AI score, override it, or reject
the evidence as misattributed. The decision store is keyed by content hash
(entity_id + query_id + quote_normalized) so decisions survive re-runs.

This stage is required to make the output publishable. AI scores alone are
demo-grade; reviewed scores are the publishable artifact.

## §7 Member-level aggregation

For each (member, query) pair, combine all reviewed evidence into a single
"cell score":

`cell_score = weighted_mean(final_score, by=evidence_weight)`

where `evidence_weight` is 1.0 for the most recent release on this query and
decays linearly to 0.3 over 6 months. The intent: a member's current stance
matters more than statements from a year ago.

If a member has zero evidence for a query, the cell is null (not zero —
absence is not the same as taking no position).

## §8 Issue-level lobbying context

For each policy query, join lobbying filings by issue code and time period to
produce an "issue lobbying brief":

- Total lobbying spend on the issue codes mapped to this query
- Top 10 clients by spend
- Top 10 filer organizations
- Sample of `specific_issues/description` text from the filings

This is independent of any specific member — it characterizes the lobbying
landscape on the policy.

## §9 Per-member publishing

For each tracked member, generate a profile page (HTML) showing:

- Header: name, party, state, chamber, count of relevant press releases
- Per-query scorecard: for each of the 5 policy queries, the cell score,
  the count of evidence pieces, and the top 3 quotes
- Lobbying context callout: for each query, the top 3 lobbying clients on
  the related issues, with their total spend

The page does NOT claim the member is "influenced by" the lobbyists. It
shows the rhetorical and lobbying landscape side-by-side and lets the
reader form a hypothesis worth investigating.

## §10 Comparative views (out of scope for v1, noted here)

A v2 would add: per-query rankings of all members; party-level aggregations;
top-changers (members whose stance shifted between two time slices); and
cluster analysis (members who consistently align on a basket of queries).
These are easy bolt-ons given the cell-aggregation output exists.

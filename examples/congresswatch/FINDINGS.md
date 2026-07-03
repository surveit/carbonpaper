# CongressWatch — what broke, what felt wrong

Findings from porting the LobbyMap-shaped pipeline to a new domain (US
Congress lobbying + press) with real data. Written as evidence for the
RETHINK memo, not as a complete review.

## 1. Data shape vs. methodology shape

### 1a. The "entity = source of document" assumption broke

LobbyMap implicitly assumes a document's `entity_id` is the entity whose
stance the document expresses. ExxonMobil's annual report expresses
ExxonMobil's stance. Clean.

In Congress data, this only holds for press releases. **Lobbying
disclosures have no member-of-Congress entity attribution.** A lobbying
filing is keyed on the filer's organization and the client; the member
of Congress being lobbied is not named. I had to fork the document
corpus into two separate input stages with two different entity-keying
strategies (member-attributed press releases vs. issue-attributed
lobbying filings) and *only* extract per-entity evidence from the first.

The prototype has no first-class concept of "document with no per-entity
stance, but useful for context." I worked around it by making
`lobbying_by_query` a separate `python_frame_function` that aggregates filings
by query but never reaches per-member granularity. **The pipeline tree
no longer fans into a single per-entity score; it has two parallel
streams that meet at publish.**

### 1b. Real data is dirtier than the prototype assumes

- 136 duplicate `filing_id` rows in the lobbying CSV. LDA filings can be
  amended (`1A`) or terminated (`1T`) with the same houseID. I treated
  houseID as primary key; in reality you need `(houseID, reportType)` or
  a composite. The schema validator caught this as a warning but it's
  the kind of thing that should be flagged at slice-build time.
- 16 filings with empty `filer_org`. Real LDA XML allows this. The
  pipeline's `nullable: false` declaration was wrong; I'd have to make
  it nullable or filter at slice time.
- Income values look numeric in the CSV but pandas auto-coerces to
  float on read, breaking my string-aware `_parse_money` regex. **The
  Python transform contract doesn't tell the function what dtype to
  expect.** Either the runtime should coerce per the declared schema,
  or the function should be hardened to handle dtype drift.

### 1c. There's no neutral benchmark

LobbyMap leans on IPCC as an objective external benchmark — that's the
source of legitimacy for the scoring direction. For most domestic policy
issues (ACA tax credits, drug pricing, Medicaid), there is no
unanimous-expert benchmark. I had to stipulate the stance axis per query
(`+2 = support extending`, `−2 = oppose extending`) and flag that as a
framing choice in the methodology doc.

The schema didn't help me make this clear: `benchmark_library` is just
text. I added `kind: stance_axis` (vs. the LobbyMap default
`expert_consensus`) but the runtime doesn't render that distinction
anywhere. **A reader of the per-member profile cannot tell that the
score axis is stipulated rather than fact-derived.**

## 2. The mock LLM is brittle in ways that matter

### 2a. Keyword matchers flip stance on quoted opposition

Press releases routinely quote the opposite side to attack it:

> "Donald Trump and Congressional Republicans failed to stop the
> expiration of the enhanced premium Affordable Care Act tax credits…"

This is a Democrat *supporting* the tax credits, criticizing the GOP
for letting them expire. The mock regex sees "expire" + "ACA tax
credits" and labels it as opposing extension. ~20% of the queue items I
reviewed by eye were mock-LLM stance flips of this kind.

This is mock-LLM fragility, but it's worth recording because it shows
**the pipeline's UI doesn't surface "this is a mock, your numbers are
not reality" loudly enough.** A real journalist seeing `D000563:
+0.21 on ACA tax credits` won't think "that score is mock-LLM noise";
they'll think Durbin's actual stance is weakly supportive.

### 2b. Stance scoring on averaged evidence destroys signal

I saw Durbin with 14 evidence pieces on Q1, cell_score = +0.21. Reading
his press releases, his stance is strongly pro-extension. The
weighted-mean aggregation washes that out because individual pieces of
evidence land at 0 or +1 with occasional −2 flips. **A score near zero
can mean either "neutral" or "noisy", and the published profile gives
no way to distinguish.**

Possible fixes: include direction proportion (% positive vs. negative
evidence) alongside the mean; show confidence intervals; show count of
extreme vs. middling evidence. The current cell_score is a single
lossy number.

## 3. Methodology compilation worked, but the seams showed

### 3a. Compiler notes are noise once you've reviewed them

Every LobbyMap stage has 2-5 compiler notes flagged. I followed the
same pattern for CongressWatch and ended up with similar quantities.
After one pass-through, those notes are stale: they either got resolved
(I made a decision and moved on) or they're permanent caveats that
shouldn't be visible on every page view. **There's no concept of
"acknowledged" or "dismissed" for compiler notes.** Same for the ⚠
badge in the DAG — it lights up on every stage with any compiler note,
making the badge meaningless.

### 3b. Inputs schema is duplicated three places

For each upstream stage, the downstream YAML re-declares the input's
schema. Then the upstream YAML declares its output_schema. Then the
python_frame_function sees the actual dataframe with potentially-different
dtypes after parquet round-trip. **Three places to keep in sync.** When
I added `most_recent_evidence` to cell_aggregation's output, I had to
update its declaration AND the publish stage's input declaration AND
the publish_member_profiles function.

In practice the runtime doesn't enforce that an input schema declaration
matches the upstream output schema — it just runs validation against
whatever's there. So the duplication is essentially documentation that
can drift silently.

### 3c. The five-stage healthcare slice is too narrow

I picked 5 policy queries focused on healthcare. Most members issued
press releases on healthcare in January 2026 (because of the ACA
expiration), so the data was rich. But this is luck. For most
member/topic combinations, evidence count is 0 — the published profile
has nothing to say. The cell-aggregation outputs 227 cells out of a
possible 218×5 = 1090. **The methodology produces empty rows for
absences.** Whether that's "this member doesn't care about insurance
regulation" or "this member just didn't issue a press release that
month" is indistinguishable.

LobbyMap has the same gap but it's hidden because companies file
financial disclosures quarterly that touch every category. Members of
Congress speak on what's in the news that week. **The temporal grain
of the data and the temporal grain of the methodology don't line up
for political actors.**

## 4. Pipeline runtime caught the right things

Things the prototype handled well, worth noting so we don't fix what
isn't broken:

- Halt-on-review + resume worked perfectly end-to-end. Approved 63
  items via the API, resumed, got 218 HTML profiles.
- Schema validation flagged the duplicate filing_id and the nullable
  violation immediately on the first run — both real-data quirks I'd
  have missed without the validator.
- Content-hash decisions: I can re-run after upstream edits and
  prior reviewer decisions carry forward by content match (assuming
  the LLM produces the same quote).
- The DAG view is great for understanding the topology. The status
  colors (green/yellow/red, pending greyed) immediately conveyed run
  state when the pipeline halted.

## 5. The journalism story the pipeline produced

Despite all the above, the run did produce a usable artifact. Looking
at Durbin's profile (M:D000563):

- 14 evidence pieces on ACA tax credits — most quoting his floor speeches
- Lobbying context: 3,840 health-issue filings; top clients PhRMA,
  UnitedHealth, AOA, all heavyweights
- A journalist could legitimately ask: "Durbin is publicly defending
  ACA tax credits while PhRMA spent N on lobbying the same issue —
  what's the relationship?"

That's the kind of question the prototype is *supposed* to make
trivial. So the shape is right even if the numbers are noisy.

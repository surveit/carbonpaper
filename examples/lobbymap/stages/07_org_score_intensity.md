---
stage_number: 7
stage_id: org_score_intensity
stage_name: Org Score + Engagement Intensity computation
source_doc: examples/lobbymap/methodology_raw.txt
source_lines: [353, 365]
related_sections: ["§2.3 (Table 2 — metric definitions, line 218)", "§4.7 (cell weights)", "§4.8 (intensity)"]
---

# Stage 7: Org Score + Engagement Intensity computation

## Prose excerpt

### Line 218 (§2.3 — metric definitions; shared with Stage 9)

> **Organization Score (0–100)** — Organization Score is a measure of how supportive or obstructive the company's direct engagement is towards science-aligned climate policy. A score of 0 would indicate full opposition, and a score of 100 equates to full support. Scores above 75 indicate broad consistency with, and support for, science-aligned policy for delivering the Paris Agreement's goal of delivering as close to 1.5°C warming as possible. Scores below 50 indicate increasingly significant misalignment between the company's detailed climate policy engagement and policy that can meet the Paris Agreement's warming targets. Scores between 50 and 75 indicate mixed engagement with such policy. If limited evidence has been collected on a company's direct policy engagement, the Organisation Score is signified with an "n/a" (not available).
>
> **Engagement Intensity (0–100)** — An independent measure of how active a company or industry association is in its direct climate policy engagement activities. This metric is independent of the Organization Score and Relationship Score and is "policy position agnostic." It provides a useful measure of the strategic importance an organization places on climate policy within its advocacy program. This metric applies equally to both companies and industry associations. A score ranging from 0 to 100 indicates the intensity of policy engagement. A score above 12 indicates active engagement, while a score above 25 indicates highly active or strategic engagement. A score below 12 indicates relatively limited engagement. Entities with Engagement Intensity scores below 5 are not attributed an Organization Score.

### Lines 353–359 (§4.7 — Org Score weighting & redistribution)

> InfluenceMap's data-content management system calculates each entity's Organization Score from the scored evidence within each cell, with weightings to factor in the relative importance of the different data sources and queries.
>
> Across the data sources, regulatory consultations (D4) and management messaging (D6) carry the highest weight. Across the queries, higher weightings are applied to policy-specific queries (Q5-Q9) than high-level statements (Q1-Q4). This system also allows for sector variation. For example, the automotive sector will have a stronger weighting assigned to Q11 (GHG Emissions Standards) as this category includes vehicle emissions standards.
>
> Graphic 7 shows an example of weights applied to the matrix. Query and data source weights result in cell-specific weights, which are demonstrated by shading—deeper shades of blue indicate that the cell is weighted more highly in the overall calculations.
>
> When "Not Scored" (N/S) or "Not Applicable" (N/A) are applied to a cell, the algorithm redistributes the weighting for that cell equally among the remaining cells in that query. The weightings thus always total to 100%, and the lack of information or non-relevance of the data source has no net impact on the organization's top-line metrics.
>
> Evidence is collected up to five years before the date of the assessment, with the most recent evidence carrying the most weight. Cells that contain no evidence that is more recent than five years are automatically excluded from the overall calculations.

### Lines 360–365 (§4.8 Engagement Intensity)

> Separately and additionally to the benchmarking process that determines the alignment of an organization's climate policy engagement with science-based pathways for delivering the Paris Agreement's goals, the assessment process seeks to determine the level or intensity of a company's policy engagement activities. This can be considered a "neutral" measurement as it is not impacted by whether the engagement is supportive or oppositional to climate policy.
>
> Engagement intensity is assessed as follows:
>
> - Each evidence item is assigned an "evidence intensity" based on the scores and meta-data attributed to it, as described above (-2 to +2 score, importance, flags, age of the evidence). Each cell in the matrix-based system described in Section 4.6 above is assigned a "cell intensity" by adding the evidence intensity of the evidence items.
> - The overall "engagement intensity" is a sum of the cell intensities, weighted by the cell weights. Cell weights recognize the relative importance of the data sources and queries and allow for sector variation. Across the data sources, regulatory consultations (D4) and management messaging (D6) carry the highest weight. Across the queries, higher weightings are applied to policy-specific queries (Q6–Q11) than to high-level statements (Q1–Q4).
>
> The resulting policy engagement metric describes the extent to which a company is engaged on climate policy. High intensity metrics indicate "strategic" levels of engagement, which reflect entities utilizing multiple engagement channels to advocate across a range of climate-related policy streams. Additional information about how to understand the engagement intensity metric can be found in Section 2.3.

## Notes for the compiler

**Explicit parameters:**
- Org Score scale: 0–100. Bands: 0–50 misalignment, 50–75 mixed, 75–100 supportive.
- Engagement Intensity scale: 0–100. Thresholds: <5 → no Org Score; <12 limited; 12–25 active; >25 strategic.
- Source weights: D4 (regulatory consultations) and D6 (management messaging) highest. Numerical weights not given.
- Query weights: policy-level queries weighted higher than high-level. (Note prose inconsistency: §4.7 says "Q5–Q9" while §4.8 says "Q6–Q11" — both refer to policy-level vs high-level partition, but indices disagree.)
- Sector variation: weights can be tuned per sector (auto sector example: Q11 / GHG emissions standards boosted).
- N/S and N/A redistribute weight *within the same query row* equally across remaining cells; weights always sum to 100%.
- 5-year cutoff; cells with no evidence newer than 5y excluded.
- Engagement Intensity = Σ (cell_intensity × cell_weight).

**Implicit / ambiguous:**
- Exact numerical weight tables are not given (only relative ranking).
- Evidence-intensity → cell-intensity → engagement-intensity formulas are described qualitatively. No 0–100 normalization function specified.
- Sector → weight mapping table not provided; just an example.
- Org Score derivation from cell scores is the most underspecified piece — we know weights and N/S handling, but not the aggregation formula (e.g., is it a weighted mean of cell scores rescaled from -2..+2 to 0..100?).

**Cross-references:** Cell scores from Stage 6 are inputs. Org Score + Engagement Intensity feed Stage 9 (Performance Band). Industry-association Org Scores from this stage are reused in Stage 8 to compute the Relationship Score.

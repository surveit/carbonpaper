---
stage_number: 6
stage_id: cell_aggregation
stage_name: Cell aggregation
source_doc: examples/lobbymap/methodology_raw.txt
source_lines: [338, 359]
related_sections: ["§4.6 (matrix architecture)", "§4.7 (weights)", "§4.8 (intensity, partial)"]
---

# Stage 6: Cell aggregation

## Prose excerpt

### Lines 338–359 (§4.6 The matrix data architecture & cell scores)

> To effectively analyze and understand this level of complexity, it is necessary to arrive at meaningful high-level metrics and summary assessments for each company and industry group. To organize and process the data, InfluenceMap has designed a unique data architecture supported by bespoke software based on a MySQL database system, with both operator content management systems (CMS) and user-friendly displays to communicate the results.
>
> Unilever's profile, shown in Graphic 3, provides an example of both the CMS and user displays for an identical part of the platform. It shows the matrix feature of the assessment software that allows for efficient archiving and clear communication of large amounts of data.
>
> This matrix structure and hierarchical organization of data allows for a flexible and scalable platform, making the information clearly available for a range of users. It includes:
>
> - High-level metrics summarizing overall results of the analysis in a simple and comparable way, covering assessments of the intensity and alignment of the company's direct policy engagement, as well as an analysis of its indirect policy engagement via industry associations.
> - A summary of InfluenceMap's scoring, which explains the analysis of each entity and offers an overview of the key data points impacting the high-level metrics.
> - The matrix in the public-facing graphic, which splits the climate agenda into InfluenceMap's queries in the far left column, with the data sources labeled along the top row.
>
> Each cell, representing the intersection of a data source column and a policy query row in the matrix structure, contains evidence collected on a company's engagement with that query from that data source. The "Cell Score" is an intermediate metric based on the individual scores of the evidence pieces in the cell, their importance, and their year. Older evidence pieces carry less weight in the system, and so only those from the last three years contribute significantly to the current cell score.
>
> Graphic 5 shows the inside of a cell within the matrix of Unilever's profile. Each cell in the matrix has its own score, which is computed from the evidence contained within it and is weighted for factors such as the date and significance of the evidence. Public users may view this cell by clicking on the relevant matrix cell online.
>
> The terms NS and NA refer to "Not Scored" and "Not Applicable," respectively. Not Scored is given when no evidence is found for that cell. Not Applicable is given when certain data sources do not apply, such as when industry groups do not disclose to CDP or financial regulators.
>
> Web links to the evidence source are provided in the bottom left of the screen, as well as time-stamped downloaded PDF files in case the page has been removed. "Extract from Source" gives an exact quote from the document that InfluenceMap is scoring, while "InfluenceMap Comment" gives a human-generated comment above it.
>
> The range of tags and "intermediate metrics" (metrics used by InfluenceMap to calculate the entity's final top-line metrics) can be seen by switching to the operator CMS screen view of this same matrix cell.

### Lines 358–359 (recency cutoff — also relevant to Stage 7)

> Evidence is collected up to five years before the date of the assessment, with the most recent evidence carrying the most weight. Cells that contain no evidence that is more recent than five years are automatically excluded from the overall calculations.

### Line 363 (cell intensity; shared with Stage 7)

> Each evidence item is assigned an "evidence intensity" based on the scores and meta-data attributed to it, as described above (-2 to +2 score, importance, flags, age of the evidence). Each cell in the matrix-based system described in Section 4.6 above is assigned a "cell intensity" by adding the evidence intensity of the evidence items.

## Notes for the compiler

**Explicit parameters:**
- Matrix shape: rows = queries (Stage 3 output), cols = data sources (Stage 2 output).
- Cell Score is a function of: per-evidence score (-2..+2), importance (0–10), and year/age.
- Recency: 5-year window; "only those from the last three years contribute significantly" (so weights decay with age, with a notable elbow at 3y and a hard cutoff at 5y).
- Cell intensity = sum of evidence intensities in the cell (additive, unbounded).
- N/S = "Not Scored" (no evidence); N/A = "Not Applicable" (e.g., industry assoc × CDP, industry assoc × Financial Disclosures).

**Implicit / ambiguous:**
- The exact weighted-mean formula for Cell Score is not given. "Significantly" is vague.
- The "evidence intensity" formula is not stated explicitly — only the inputs (-2..+2, importance, flags, age).
- No spec for how flags/stars (Stage 5) modulate either Cell Score or cell intensity; only "evidence intensity" mentions flags as input.
- Behavior when a cell has only "0 / no position" evidence — score is 0 but presumably contributes intensity.

**Cross-references:** Output feeds Stage 7 (org-level Org Score and Engagement Intensity, weighted by sector/source/query weights). N/S redistribution rule lives in Stage 7 (line 357).

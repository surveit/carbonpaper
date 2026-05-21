---
stage_number: 5
stage_id: importance_tagging
stage_name: Importance + tag assignment
source_doc: examples/lobbymap/methodology_raw.txt
source_lines: [331, 337]
related_sections: ["§4.4 (tail of section, 'In addition to scoring above...')", "§4.5 (lines 335–337)"]
---

# Stage 5: Importance + tag assignment

## Prose excerpt

### Lines 331–337 (Tagging and secondary metrics; transition into §4.5)

> In addition to the scoring process above, the LobbyMap assessment process applies tags and records secondary metrics to capture various types of additional information within each evidence item assessed. These include:
>
> - Information on the region and date of each evidence item is recorded so the dataset can be analyzed from various angles, such as variation over time and region. Similarly, tags are applied to log evidence that refers to specific policies/laws, technologies, fuel types, or key themes, such as environmental justice and a just transition.
> - The "importance" of each evidence item is also assessed on a scale from 0 to 10. This captures the relative significance of an evidence piece compared to others from the same data source that cover the same policy issue. This helps distinguish between evidence of high-level statements with limited potential impact and evidence of direct and detailed engagement with policymakers.
> - Flag or star tags highlight positive and negative evidence pieces of high significance to the system's users. Principally, flags and stars signify an item of evidence that shows strategic policy engagement from the company in either a highly supportive or oppositional manner.
>
> The LobbyMap platform captures and assesses over 30,000 items of evidence each year, producing metrics and analysis on the climate policy engagement activities of the 1000 companies and 330 industry associations in the system. The following section describes the practical details of the assessment process and its implementation.
>
> As outlined above, the primary sources of this evidence consist of a varied range of structurally irregular documents, text, and recorded commentary. All of these sources are stored on the LobbyMap system, requiring the archiving of large volumes of data in PDFs, text comments, and external URLs. As of January 2024, the system has archived over 150,000 evidence pieces online.
>
> Each item of relevant evidence from these documents is assessed and tagged according to the processes described above, producing multiple useful data points. As such, in aggregate, the LobbyMap platform contains well over a million datapoints on corporate and industry climate policy engagement globally.

## Notes for the compiler

**Explicit parameters:**
- Per-evidence importance: integer (presumably) on 0–10 scale, scoped *relative to other evidence in the same (data source × policy issue) bucket*.
- Recorded fields per evidence: region, date, importance (0–10), policy/law tags, technology tags, fuel-type tags, theme tags (e.g., environmental justice, just transition), flag/star designation.
- Flags/stars are binary high-significance markers — "highly supportive" vs "oppositional" strategic engagement.
- Volume context: ~30,000 evidence pieces/year ingested; 150,000+ archived as of Jan 2024.

**Implicit / ambiguous:**
- Importance is "relative to others from the same data source that cover the same policy issue" — does this mean importance is normalized within a (source, query) cell, or assessed and *then* used relatively? Compiler should treat as raw 0–10 with cell-relative semantic.
- The closed vocabulary of tags (which fuels? which technologies? which policies?) is not in the prose — there's an open-ended controlled list.
- Distinction between "flag" and "star" is unclear — prose treats them as the same construct ("flag or star tags").
- Date format and region taxonomy not specified.

**Cross-references:** Importance and date feed into Stage 6 cell aggregation weights (recency decay over 5 years; lines 358). Flag/star plus importance feed into "evidence intensity" used in Stage 6/7 intensity computation (line 363).

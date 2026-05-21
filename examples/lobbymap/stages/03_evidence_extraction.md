---
stage_number: 3
stage_id: evidence_extraction
stage_name: Evidence extraction & relevance filter
source_doc: examples/lobbymap/methodology_raw.txt
source_lines: [253, 262]
related_sections: ["§3.3", "§4.1 intro (lines 286–291)", "Appendix B (lines 428–433)"]
---

# Stage 3: Evidence extraction & relevance filter

## Prose excerpt

### Lines 253–262 (§3.3 Defining and Categorizing Climate Policy / Table 4)

> The UNFCCC process (including IPCC guidance) has triggered climate-motivated policy and regulatory processes from government regulators. The Paris Agreement commits signatories to develop Nationally Determined Contributions (NDCs), in which nations outline their implementation plans to meet the Agreement's goals. The LobbyMap system considers existing, evolving, and likely future policy measures issued by mandated bodies. "Mandated bodies" are defined here as various levels of government or government-authorized bodies that are tasked with NDC implementation in their regions.
>
> **Table 4: InfluenceMap Queries Based on Subcategories of the Climate Policy Engagement Agenda**
>
> | Type | Policy Category | Description |
> |---|---|---|
> | High-level | Climate Science | Transparency around climate change science |
> | High-level | Climate Science Stance | Position on the needed response to climate change science |
> | High-level | Need for Climate Regulation | Support for regulations to tackle climate change in general |
> | High-level | The UN Climate Process | Support for the UNFCCC process on climate change |
> | Policy-level | Carbon Tax* | Support for policies/regulation on this topic |
> | Policy-level | Emissions Trading* | Support for policies/regulation on this topic |
> | Policy-level | Energy & Resource Efficiency | Support for policies/regulation on this topic |
> | Policy-level | Renewable Energy* | Support for policies/regulation on this topic |
> | Policy-level | Energy Transition & Zero Carbon Technologies* | Support for policies/regulation on this topic |
> | Policy-level | GHG Emission Regulation | Support for policies/regulation on this topic |
> | Policy-level | Carbon Sinks | Support for policies/regulation on this topic |
> | Disclosure & Transparency | Direct Policy Engagement | Transparency on positions and activities to influence climate policy/legislation |
> | Disclosure & Transparency | Indirect Policy Engagement | Transparency on industry association relationships and their policy engagement activities |
>
> *Following consultation, InfluenceMap will consider updating the categorization of policy that currently fits into these subcategories to better reflect the evolving real-world climate policy context.
>
> The climate policy measures covered in the assessment encompass a spectrum of actions, ranging from high-level statements of intent to detailed and prescriptive legislation. They include the establishment of targets, implementation standards, fiscal interventions, and other binding regulatory requirements. Climate considerations are also increasingly influencing policy areas that are not traditionally associated with climate, such as building codes, land use policy, trade policy, and fiscal regulations. The climate-related components within these policies fall under InfluenceMap's definition of "Climate Policy."
>
> InfluenceMap breaks down the climate policy engagement agenda into a series of subcategories. The evidence we gather of corporate engagement with these climate policy subcategories builds a full picture of corporate interaction across the climate policy agenda.
>
> The "high-level" categories capture engagement with broad issues that inform the wider context and narratives surrounding climate policy action. The "policy-level" categories capture engagement on specific legislative and regulatory strands that may be present at a regional level. For example, the "Carbon Tax" category archives corporate engagement with a range of policy strands in different regions that function as a tax on CO 2 emissions.
>
> Additionally, two subcategories are employed to assess the clarity, accuracy, and comprehensiveness of corporate governance and climate policy engagement disclosures. Evidence in these subcategories is benchmarked against the indicators outlined in the Global Standard on Responsible Climate Lobbying.
>
> The above queries are altered for InfluenceMap's assessment of the financial sector. Please see Appendix B for further details.

### Lines 286–291 (§4.1 intro — also relevant to Stages 4 and 6; shared with Stage 4)

> The LobbyMap platform's corporate and industry association assessments depend on the aggregation of up to several hundred individually assessed items of evidence collected on each entity's climate policy engagement activities. The range of data sources and different categories of climate-related policy used to collect and organize this evidence are described in Section 3. This chapter explains the assessment that is applied to the evidence.
>
> InfluenceMap's analysis of climate policy engagement incorporates two types of assessment:
>
> - An assessment of the "alignment" of an entity's policy positions and advocacy with policy to deliver the Paris Agreement's goal to "avoid dangerous climate change by limiting global warming to well below 2°C and pursuing efforts to limit it to 1.5°C."
> - An assessment of the "intensity" of an entity's advocacy and engagement positions to promote these positions.

### Lines 428–433 (Appendix B — financial-sector query variant)

> InfluenceMap uses an altered set of queries for finance sector companies and industry associations to incorporate an analysis of both climate-related financial policy and real-economy climate policy. All other aspects of the methodology remain the same.
>
> Climate finance queries were drawn from the United Nations Environment Programme's 2015 recommendations The Financial System We Need and the findings of the EU's 2016 High-Level Expert Group on Sustainable Finance, then refined with input from experts in the field.
>
> Real-economy queries are consolidated versions of equivalent climate change queries: Real Economy Climate Regulations cover Carbon Tax, Emissions Trading, Energy & Resource Efficiency, and GHG Emission Regulation; Energy, Industry and Land Transitions cover Renewable Energy, Energy Transition & Zero Carbon Technologies, and Carbon Sinks.

## Notes for the compiler

**Explicit parameters:**
- Standard taxonomy: 13 queries split into High-level (4), Policy-level (7), Disclosure & Transparency (2). User spec says "14 policy queries" — likely counting D&T entries plus the 11 substantive + extras; canonicalize against Table 4 (literal count = 13).
- Finance sector substitutes a different query set (Table 10, lines 432–433) — 11 queries with consolidated Real-Economy categories.
- Disclosure & Transparency rows are benchmarked against the Global Standard on Responsible Climate Lobbying, not IPCC.

**Implicit / ambiguous:**
- The actual evidence-extraction step (how operators identify candidate evidence pieces from a source document) is not described procedurally — only the resulting taxonomy.
- Relevance filter logic (i.e., assigning an evidence piece to exactly one query vs. multiple) is unstated.
- "Mandated bodies" definition is qualitative.

**Cross-references:** Output of this stage feeds Stage 4 (per-evidence scoring) and is the row dimension of the matrix in Stage 6.

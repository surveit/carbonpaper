---
stage_number: 1
stage_id: universe_selection
stage_name: Universe selection
source_doc: examples/lobbymap/methodology_raw.txt
source_lines: [263, 279]
related_sections: ["§3.4", "§1 (intro stat)"]
---

# Stage 1: Universe selection

## Prose excerpt

### Lines 157 (intro stat from §1 — provides high-level summary; shared with Stage 8)

> As of September 2024, the LobbyMap platform covers approximately 600 companies and 300 leading industry groups. The process for selecting companies prioritizes the largest companies as determined by the Forbes Global 2000, as well as factors such as sector and regional headquarters. A key area of future growth for the LobbyMap platform relates to companies headquartered in low and middle income countries that will be critical for addressing the climate crises, including China, Brazil, India, Indonesia, Mexico, and South Africa.

### Lines 263–279 (§3.4 Selection of Companies and Industry Associations)

> As of September 2024, the LobbyMap platform covers approximately 600 companies and 300 leading industry groups, while also tracking the connections between them.
>
> The extensive nature of the LobbyMap analysis has previously presented challenges to scaling the universe of analyzed companies in line with other organizations collecting data on corporate climate performance (for example, CDP's database covers nearly 15,000 companies).
>
> However, with advancements in new artificial intelligence (AI) and machine learning techniques, InfluenceMap is exploring ways to enhance the research process's efficiency without compromising the depth and accuracy of the research. Our current objective is to reach 1000 companies in 2025.
>
> Our system for selecting companies prioritizes the largest companies as determined by the Forbes Global 2000, which aggregates a range of indicators relevant to economic size and political influence. The LobbyMap platform initially selects companies based on an average of their performance on the Forbes 2000 list over the past three years. Approximately 50% of the companies in InfluenceMap's database rank higher than 500 on their average Forbes ranking over this time, with 70% ranked higher than 1000.
>
> In addition to size, the selection process has considered each company's likely relevance to climate change in LobbyMap's regions of focus. As a result, the database emphasizes:
>
> - Companies in sectors pivotal to the global energy transition, including energy, utilities, transport, heavy industry (chemicals, steel, cement, etc.), mining, and agriculture.
> - Companies from any sector flagged as potentially engaged on climate policy by screening and aggregating memberships in corporate climate initiatives worldwide.
> - Companies headquartered or operating in our focus regions, which are Canada, the United States, Europe, South Korea, Japan, and Australia.
>
> A key area of future growth for the LobbyMap platform relates to companies headquartered in low and middle income countries with large economies that will be critical for addressing the climate crises. In particular, we are currently in the process of broadening our focus to include China, Brazil, India, Indonesia, Mexico, and South Africa.
>
> LobbyMap is also expanding its coverage of finance sector companies and industry associations, with a slightly modified methodology (see Appendix B).
>
> InfluenceMap refers to industry groups, business federations, and other similar groups representing corporate interests as "influencers." Where possible, InfluenceMap captures the additional influence that companies have via third-party groups. This analysis currently focuses on the role of industry associations for the following reasons:
>
> - InfluenceMap's analysis focuses at the company level, capturing third-party groups when their relationships with companies can be accurately and consistently tracked. While this holds true for most industry groups, many professional policy engagement consultancies and think tanks operate under limited disclosure regimes, hindering such analysis.
> - Industry policy engagement groups often act on behalf of entire sectors or, in some cases, entire economies. Consequently, they possess substantial leverage to influence the passage of climate policy within the regions in which they operate.
>
> In selecting the scoring universe for industry groups, the following criteria are used:
>
> - An assessment of the relevance of climate issues to the industry group and how active the group is likely to be on climate change matters. In alignment with our company selection, this step prioritizes groups representing key climate sectors, including energy, utilities, transport, industrial, heavy industry (chemicals, steel, cement), and agriculture. We also consider cross-sector business federations (e.g., the US Chamber of Commerce) or relevant issue-specific cross-sector groups (e.g., an "energy-intensive industry alliance") as highly significant when engaging on climate issues.
> - An estimation of the industry group's influence within its jurisdiction, be it in the US, the EU, Japan, or on the international stage. This assessment factors in the group's size and the size and significance of the companies or sectors it represents. This evaluation is conducted by surveying and aggregating the perspectives of businesspeople, policymakers, and civil society groups familiar with the jurisdiction and the group's political influence.
> - The "jurisdiction weighting" accounts for the size of the economy and the contribution of the industry group's jurisdiction to global greenhouse gas emissions and exported fossil fuels.

## Notes for the compiler

**Explicit parameters:**
- Forbes Global 2000 ranking, averaged over past 3 years, is the size filter. Distribution targets noted (50% < rank 500; 70% < rank 1000).
- Sector emphasis: energy, utilities, transport, heavy industry (chemicals, steel, cement), mining, agriculture.
- Focus regions: Canada, US, Europe, South Korea, Japan, Australia. Expansion regions: China, Brazil, India, Indonesia, Mexico, South Africa.
- Industry-association selection: 3 criteria — sector relevance, jurisdictional influence (via stakeholder survey), jurisdiction weighting.

**Implicit / ambiguous:**
- "Climate-initiative membership screen" is mentioned but the initiatives aren't enumerated.
- Numerical thresholds for sector tags / climate-relevance flags aren't given.
- "Jurisdiction weighting" is described qualitatively — no formula. The stakeholder survey methodology is also informal.
- Finance sector uses modified queries (Appendix B, lines 428–433) but same selection logic.

**Cross-references:** Industry-association selection feeds Stage 8 (relationship graph). Company-level intensity threshold for Org Score (intensity < 5) is set in Stage 7, not here.

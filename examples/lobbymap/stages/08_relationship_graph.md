---
stage_number: 8
stage_id: relationship_graph
stage_name: Industry-association ↔ company relationship graph
source_doc: examples/lobbymap/methodology_raw.txt
source_lines: [366, 389]
related_sections: ["§4.9", "§3.4 (industry-assoc selection — see Stage 1)", "§2.3 Relationship Score definition (line 218)"]
---

# Stage 8: Industry-association ↔ company relationship graph

## Prose excerpt

### Line 218 (§2.3 — Relationship Score definition; shared with Stage 9)

> **Relationship Score (0–100)*** — Relationship Score is a measure of how supportive or obstructive the company's industry associations are towards science-aligned climate policy. The Relationship Score is an aggregate assessment of the climate policy engagement of a company's industry associations. This calculation accommodates an assessment of the strength of the relationship between a company and an industry association. For example, a stronger weighting will be attributed where a company has a representative on the board of an industry association. A score of 0 would indicate full opposition, and a score of 100 equates to full support. Scores above 75 indicate broad consistency with, and support for, science-aligned policy for delivering the Paris Agreement's goal of delivering as close to 1.5°C warming as possible. Scores below 50 indicate increasingly significant misalignment between the detailed climate policy engagement of a company's industry associations and policy that can meet the Paris Agreement's warming targets. Scores between 50 and 75 indicate mixed engagement with such policy. If limited evidence has been collected on a company's industry association links, the Relationship Score is signified with an "n/a" (not available).
>
> *Relationship Scores are generated for companies only.

### Lines 366–389 (§4.9 Assessing Indirect Engagement via Industry Associations)

> InfluenceMap maintains a database of over 300 industry associations, federations, and advocacy groups (collectively referred to as "influencers'' in the LobbyMap system).
>
> The core role of many industry associations is to represent the interests of their corporate members to governments, with a particular emphasis on processes associated with the formation of policy and regulation.
>
> Industry groups organize themselves in numerous ways, but common groupings include by sector (for example, an automotive or steel sector association), by region (for example, a cross-sector business federation such as the US Chamber of Commerce representing the business voice in that region), or by cross-cutting issue (for example, such issues might include climate change or energy intensive industries).
>
> Industry associations constitute a critical part of the corporate policy engagement landscape. InfluenceMap's analysis has consistently found them to play active and impactful roles in influencing climate policy. Key contributing factors to their role include:
>
> - Well-resourced and finely-tuned policy influencing operations, often backed by deep institutional and political relationships that they have formed over the many decades of their existence.
> - Deep technical knowledge and expertise about their sector or industry, often to an extent that is far superior to that of their civil society counterparts or public policy institutions.
> - An ability to talk on behalf of an entire sector or economy and the jobs and growth that these represent when promoting specific positions, a factor that is highly persuasive for policymakers and politicians.
>
> As such, industry associations are widely used by companies to significantly augment their policy influence in key markets on a range of issues.
>
> **Assessing Company Indirect Influence Through Industry Associations**
>
> The LobbyMap platform's measurement of a company's "indirect" influence on climate policy via its industry associations is based on two research processes:
>
> 1. A full and independent assessment of each industry association in the LobbyMap system, following an identical methodology to the one described in the previous two chapters.
> 2. A system for tracking and measuring the "Relationship Strength" between a company and an industry association.
>
> Relationship Strength is assessed on a scale of 1–10 (from weak to strong) based on precise guidelines on how to rate the strength of a variety of relationships. This assessment considers a range of factors, including:
>
> - The size of the industry association's membership and, therefore, the company's likely significance within it (for example, a company that is a member of an industry association with fewer than 10 members will have a higher relationship score than one that is a member of an industry association with 200 members).
> - Membership to key committees or boards, which would result in a higher relationship score.
> - The involvement of senior company executives within the industry association (for example, the CEO of a company serving as the chairman of an industry association would indicate a very strong relationship link).
> - The extent to which a company has clearly and transparently communicated that it disagrees with the industry association's position on climate change. Such communications (e.g., via enhanced corporate disclosure on the topic) would weaken the relationship strength score recorded.
>
> In addition to company-industry association relationship strength, InfluenceMap also measures the "Relative Ranking" of an industry group on a 0–10 scale. This metric is an estimation of the power that the association has in its jurisdiction, such as in the US, the EU, Japan, or internationally. This is assessed with reference to the size of the group and the size and importance of the companies or sectors for which it is mandated to speak. This is done by surveying and aggregating the opinions of businesspeople, policymakers, and civil society groups familiar with the jurisdiction and the group's political influence.
>
> These sub-metrics feed into the calculation of a company's overall Relationship Score (see Section 2.3), which, in essence, measures its indirect climate policy engagement by aggregating LobbyMap's assessments of the industry associations of which it is a member. This calculation is moderated by the strength of the company's relationship to each industry association and the relative importance of each association.
>
> The figure below shows an output of the database of relationships between companies and their key industry groups. InfluenceMap updates this database on a continuous basis.
>
> Graphic 9 shows a snapshot of a company profile, with the "Details of Relationship Score" tab selected. To ensure complete transparency, the Relationship Strength assessments for all links that a company has with its industry groups are available for users to view via a simple light box display over the profile page, as indicated in Graphic 9. Selecting an industry association directly from the Details of Relationship Score selecting tab (as indicated in Figure 9) takes the user to that industry association's LobbyMap profile.

## Notes for the compiler

**Explicit parameters:**
- Relationship Strength: integer 1–10, per (company, industry-association) edge.
- Relationship Strength factors: association membership size (smaller assoc → higher strength), board/committee membership (boost), senior-exec roles (boost; CEO-as-chair = very strong), public disagreement disclosure (penalty).
- Relative Ranking: 0–10 per industry association, jurisdiction-aware, sourced via surveys of stakeholders.
- Industry-assoc Org Scores reused from Stage 7 (full independent assessment, identical methodology).
- Relationship Score: aggregate of (industry-assoc Org Score, Relationship Strength, Relative Ranking) over all associations the company is a member of, on 0–100 scale.

**Implicit / ambiguous:**
- The aggregation formula for Relationship Score is qualitative: "moderated by the strength of the company's relationship to each industry association and the relative importance of each association." No closed-form weight expression.
- The Relationship Strength rubric is described as "precise guidelines" but only example factors are listed — the full 1–10 mapping is not in the prose.
- Relative Ranking survey methodology is informal — no panel size, no calibration procedure.
- N/A handling for companies with no tracked industry-assoc memberships: prose says "n/a" if limited evidence (Stage 9 territory).

**Cross-references:** Industry-association universe and selection are upstream (Stage 1, lines 273–279). Relationship Score feeds Stage 9 (combined into Performance Band).

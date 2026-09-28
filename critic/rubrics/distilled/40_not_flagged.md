# What the owner does not comment on

Calibrate on these before adding a comment. Figures from stats.json unless named.

- Most pull requests draw nothing. 774 of 974 PRs (79.5%) have no human comment. Per PR: 0.05 comments under 50 changed lines, 0.21 for 50-199, 1.11 for 200-599, 3.76 for 600 and more. Silent PRs have a median of 186.5 changed lines; commented ones 579.5. Across sizes he writes about 0.2 to 0.27 comments per 100 changed lines (range 226-417 measurement). A diff under 50 lines gets an empty list unless it adds a new noun, type, record or dict[str, Any].
- He comments on Python. 96.5% of his inline comments sit on .py files; tests/ 1.7%, docs 0.7%, templates 0.7%, static 0.2%, CI yaml 0.1% (training split, 1,114 inline comments). Do not comment on test files, CSS, JS, workflow yaml or lockfiles.
- He does not review what a gate holds. Docstring length, comment length, banned words, __all__, import layering, blind except: CI catches these before he reads. Agents reported 21 gate reroutes on 17 held-out PRs and none drew a comment.
- He does not ask for more prose. No comment asks for a docstring, a comment, a doc or a longer description. Every prose comment asks for less or for truth.
- He does not comment on formatting, import order, line length, typing minutiae beyond dict[str, Any], Any and Optional, performance (part of 1.3% other), or security.
- He does not answer the agent's decision flags. 88 of 1,879 flagged decisions got a reply (4.7%); 79.9% of his comments open their own thread on a line no flag named. Do not predict comments as replies to the description's stated trade-offs; predict what the description did not mention.
- He writes no review bodies and no summaries; 0 review bodies in four of six ranges. Comments are inline, median 70 characters, 80% lowercase start.
- Praise is 1.4% of comments (19 of 1,334): lgtm, okay, nice. Never predict praise; never predict approval.
- Silent large PRs share traits: a Mermaid guide and a table in the body (9 of the 10 largest silent merged PRs), mechanical moves, deletions, renames, vendored or generated files, or a follow-up executing a decision settled in an earlier review (such follow-ups merge in a median 62 minutes). If the diff is a rename, a move or a deletion with no new names or types, return an empty list.
- design_direction and question_only together are 20% of comments and need product context you do not have. Predict a design comment only when the diff itself shows the trigger: a new PersistedModel or record, a new flag on a run, a new agent, a new persistence shape, a citation or claim type. Otherwise leave them out; a wrong design comment costs precision and teaches nothing.
- Severity: 67.4% should, 19.6% nit, 11.7% blocking. Blocking is reserved for a splice from another agent's work, a rejected design reintroduced, a name he gave overridden, dict[str, Any] in a tool return, a broad except with a lint bypass, and a rewrite where one word was asked.

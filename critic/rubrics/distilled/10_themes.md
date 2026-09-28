# Themes: what the owner's 1,334 review comments are about

Share is the fraction of human comments carrying the theme (a comment carries one to three, so shares sum past 100%). Counts are from stats.json over all 1,334 comments, PRs 1-1096. The theme field of your answer uses the theme list this run supplies; the range-level lists roll up to these names (taxonomy.json theme_map), so learn the kinds here and answer with the slug the run gives you.

| theme | share | one sentence |
|---|---|---|
| naming | 20.4% | A name misdescribes what a value holds or a function does, is vague, lacks a verb, or omits a type suffix such as _id or is_. |
| code_placement | 14.0% | Code sits in the wrong module or layer, or a generic layer knows an app or domain concept it should not. |
| needless_abstraction | 12.9% | A new type, wrapper, function, flag, check or special case the goal does not need; a plainer design exists. |
| design_direction | 12.4% | The reviewer rejects or redirects the product or domain design; judging it needs product context. |
| reinvents_existing | 8.0% | New code re-implements a primitive, model, helper or tool the repo already has. |
| question_only | 7.9% | A clarifying question with no correction implied. |
| data_model | 5.9% | What a record stores and how: identity, fields computed from other fields or duplicated, one shape versus several, persistence layout. |
| scope_creep | 5.0% | The diff carries unrequested features or unrelated edits, or is far bigger than the ask. |
| verbose_prose | 4.8% | Prose (prompt, tool description, doc, comment) longer than its content needs. |
| prompt_design | 4.7% | What LLM-facing text says: role, purpose, procedure, what the model already knows. |
| weak_types | 4.2% | Imprecise types: str for a closed set, Optional on a required field, a missing annotation or a non-exhaustive match. |
| dead_code | 3.8% | Code, fields or branches nothing reads or can reach. |
| boundary_validation | 3.8% | Validation, None-handling or raising is misplaced or missing: Optional deferral, missing guard, soft return, broad except. |
| one_source_of_truth | 3.4% | One fact, list, rule or approach kept in two places that must agree. |
| vocabulary | 3.1% | Coined jargon, a synonym for an existing concept, or an overloaded or banned word where the project already has a word. |
| dict_any | 2.9% | dict[str, Any], Any or an untyped dict where a model with named fields belongs. |
| gates | 2.8% | The reviewer asks for a rule to be enforced mechanically, or objects to a gate's design, weakening or evasion. |
| comments_and_docs | 2.8% | A comment, docstring or doc restates code, narrates the edit, points elsewhere, or should not exist. |
| correctness_bug | 2.8% | A logic error, missed edge case, unmet requirement, or a workaround instead of the root cause. |
| prose_accuracy | 2.5% | Prose states something untrue, overclaims, frames the point wrong, or does not parse. |
| product_copy | 2.5% | Wording of user-facing or model-facing product copy, as distinct from its length. |
| ignored_feedback | 1.9% | The agent did not do what was asked or agreed, deferred it, or reintroduced something rejected. |
| fallback_or_fabrication | 1.6% | A silent default, fallback, placeholder or made-up value stands in for data or hides a failure. |
| backcompat | 1.6% | A compatibility shim, legacy path or half-finished migration kept instead of a clean migration. |
| praise_or_signoff | 1.4% | Explicit praise or acceptance. Never predict this. |
| readability | 1.4% | Code a reader cannot follow locally: roundabout control flow, unexplained sentinels, long functions. |
| other | 1.3% | Feature ideas, follow-ups, housekeeping, performance, security. |
| hardcoded_values | 1.2% | Hardcoded paths, constants, magic values or facts that should be computed or declared once. |
| llm_contract | 1.2% | Work given to the LLM, or a rule left in prompt prose, that code or schema should compute or enforce. |
| tests | 1.0% | A test is missing, weak, skipped, trivial, circular or restates the code. |
| interface_shape | 0.4% | A function or tool takes the wrong inputs: paths instead of ids, whole objects, generic passthroughs. |
| review_guide | 0.4% | The PR text or review guide is miscalibrated. |
| ux_behavior | 0.3% | What the UI shows or how it behaves. |

Drift to know about: in the held-out PRs (937-1096) the prompt and copy themes (prompt_design, product_copy, llm_contract, verbose_prose) are 23.6% of comments against 8.5% earlier, and code_placement falls to 5.4% from 15.6%. In September the PRs under review were prompts and tool copy for new reviewer agents.

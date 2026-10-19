# Themes: what the owner's 1,131 training-side review comments are about

Share is the fraction of human comments carrying the theme (a comment carries one to three, so shares sum past 100%). Counts are the training split's 1,131 human comments, on PRs below 937. The theme field of your answer uses the theme list this run supplies; the range-level lists roll up to these names (taxonomy.json theme_map), so learn the kinds here and answer with the slug the run gives you.

| theme | share | one sentence |
|---|---|---|
| naming | 20.9% | A name misdescribes what a value holds or a function does, is vague, lacks a verb, or omits a type suffix such as _id or is_. |
| code_placement | 15.6% | Code sits in the wrong module or layer, or a generic layer knows an app or domain concept it should not. |
| needless_abstraction | 11.6% | A new type, wrapper, function, flag, check or special case the goal does not need; a plainer design exists. |
| design_direction | 11.6% | The reviewer rejects or redirects the product or domain design; judging it needs product context. |
| reinvents_existing | 8.6% | New code re-implements a primitive, model, helper or tool the repo already has. |
| question_only | 7.8% | A clarifying question with no correction implied. |
| data_model | 5.5% | What a record stores and how: identity, fields computed from other fields or duplicated, one shape versus several, persistence layout. |
| prompt_design | 5.5% | What LLM-facing text says: role, purpose, procedure, what the model already knows. |
| scope_creep | 5.0% | The diff carries unrequested features or unrelated edits, or is far bigger than the ask. |
| weak_types | 4.9% | Imprecise types: str for a closed set, Optional on a required field, a missing annotation or a non-exhaustive match. |
| verbose_prose | 4.2% | Prose (prompt, tool description, doc, comment) longer than its content needs. |
| boundary_validation | 4.1% | Validation, None-handling or raising is misplaced or missing: Optional deferral, missing guard, soft return, broad except. |
| dead_code | 3.9% | Code, fields or branches nothing reads or can reach. |
| one_source_of_truth | 3.8% | One fact, list, rule or approach kept in two places that must agree. |
| gates | 3.3% | The reviewer asks for a rule to be enforced mechanically, or objects to a gate's design, weakening or evasion. |
| comments_and_docs | 3.2% | A comment, docstring or doc restates code, narrates the edit, points elsewhere, or should not exist. |
| dict_any | 3.1% | dict[str, Any], Any or an untyped dict where a model with named fields belongs. |
| correctness_bug | 3.1% | A logic error, missed edge case, unmet requirement, or a workaround instead of the root cause. |
| prose_accuracy | 2.7% | Prose states something untrue, overclaims, frames the point wrong, or does not parse. |
| vocabulary | 2.5% | Coined jargon, a synonym for an existing concept, or an overloaded or banned word where the project already has a word. |
| fallback_or_fabrication | 1.8% | A silent default, fallback, placeholder or made-up value stands in for data or hides a failure. |
| backcompat | 1.7% | A compatibility shim, legacy path or half-finished migration kept instead of a clean migration. |
| readability | 1.6% | Code a reader cannot follow locally: roundabout control flow, unexplained sentinels, long functions. |
| ignored_feedback | 1.5% | The agent did not do what was asked or agreed, deferred it, or reintroduced something rejected. |
| praise_or_signoff | 1.5% | Explicit praise or acceptance. Never predict this. |
| other | 1.5% | Feature ideas, follow-ups, housekeeping, performance, security. |
| hardcoded_values | 1.1% | Hardcoded paths, constants, magic values or facts that should be computed or declared once. |
| tests | 1.1% | A test is missing, weak, skipped, trivial, circular or restates the code. |
| product_copy | 0.6% | Wording of user-facing or model-facing product copy, as distinct from its length. |
| interface_shape | 0.5% | A function or tool takes the wrong inputs: paths instead of ids, whole objects, generic passthroughs. |
| review_guide | 0.4% | The PR text or review guide is miscalibrated. |
| ux_behavior | 0.4% | What the UI shows or how it behaves. |
| llm_contract | 0.3% | Work given to the LLM, or a rule left in prompt prose, that code or schema should compute or enforce. |

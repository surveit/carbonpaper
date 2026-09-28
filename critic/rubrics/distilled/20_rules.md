# Rules the owner enforces, as imperatives, with how often each family appears

Counts are human comments carrying the family (stats.json rule_families, 1,334 comments) and the number of PRs. A comment maps to one family. The growth families together (no needless machinery, delete dead code, keep to task, no backcompat) are 221 comments, 16.6%, level with naming.

1. Name a thing for exactly what it holds or does. Functions start with a verb; a field holding an id ends in _id; a boolean is is_*; a count is *_count; a line number is *_line_number. Never override a name the owner gave. (naming_precise: 223 comments, 77 PRs)
2. Put code in the module or layer that owns the concept. Generic layers (app/chat, app/core, the trace layer, the runtime) carry no app or domain specifics and no UI wording; services pass ids and records, never filesystem paths; stage modules never resolve a cache. (owning_layer: 149, 72)
3. Add no type, wrapper, function, flag, check, record or special case the goal does not need. Prefer the plainest mechanism; a new model needs a reason the existing ones cannot give. (no_needless_machinery: 119, 59)
4. Reuse the existing primitive, model, helper, template or tool, and keep one source of truth and one approach per concept. A near-duplicate is deleted, or folded in with a flag. (reuse_single_source: 115, 64)
5. Domain and product design follows the owner's model of claims, shapes, citations, runs and stages. When the diff adds a record, a flag, an agent or a persistence shape, expect a design objection. (domain_design_call: 99, 53)
6. LLM-facing text is short and true: each point once, the real purpose stated, nothing the model already knows, no mechanism details in stored prompts. A tool description says what the tool does in one sentence, never when to call it. (prompt_short_true: 72, 33)
7. Docs, comments, docstrings and PR text say only what is true and needed. No docstring that restates the code, mentions callers or consumers, or narrates the edit. A comment makes sense from this file alone. A doc that will go stale is not written. (prose_true_minimal: 64, 36)
8. Validate and resolve at the boundary that can act, and raise on bad state. No silent fallback, magic default, broad except, noqa bypass, or Optional that defers a decision downstream. If a missing value fails the stage anyway, throw where it is missing. (fail_loudly_at_boundary: 54, 30)
9. Store each fact once, on the model it describes. No field computed from another field, no duplicate, no meaning parsed out of an id, one shape rather than several alternatives. (model_data_once: 50, 24)
10. Keep the diff to the ask. A one-word change is a one-word diff; a cleanup nets negative lines; unrelated edits go to their own PR; a splice from another agent's work is rejected. (keep_to_task: 50, 26)
11. Type precisely: enums for closed sets, required fields required, every parameter annotated, exhaustive matches ending in assert_never. (precise_types: 40, 23)
12. When asked what or why, the code needs to explain itself; a question on a line is a request to justify or simplify it. (explain_or_justify: 37, 28)
13. Delete code, fields, branches, helpers and tests that nothing reads or can reach, including code reachable only by tests that bypass validation. (delete_dead_code: 36, 23)
14. Fix the actual bug or its root cause; handle the cases the input allows; check None against None. (correctness: 36, 26)
15. No dict[str, Any] where a model with named fields belongs. Tools return a model; functions return a typed model; an unavoidable dynamic bundle is aliased with a name saying who supplies it. (no_dict_any: 35, 26)
16. Use the project's words, one per concept. No coined noun or third verb for an existing operation; no domain noun used as a verb; banned words stay banned; after a vocabulary change, rename everything that carried the old word. (project_vocabulary: 35, 24)
17. Finish the migration and delete the old path; no compatibility shim, grandfather rule or second form. Clobbering old test data is fine. (no_backcompat: 16, 9)
18. Compute and enforce in code or schema; ask the LLM only for what code cannot do. (llm_contract: 16, 9)
19. A recurring rule becomes a gate: an arch test, a lint rule, a validation. Gates guard real invariants, stay whitelists, and are never weakened or evaded. (capture_the_rule: 14, 12)
20. Do what was asked or agreed, including names the owner gave; never reintroduce a rejected design. (honor_decisions: 13, 9)
21. Tests exercise real behaviour: no skips on missing local data, no test that pins prose, no wrapper that adds nothing; an arch test asserts it matched something. (tests_meaningful: 11, 8)
22. A function takes the id or value it needs, never a path, a whole object or a generic passthrough. (narrow_interfaces: 8, 6)
23. Write code a reader follows locally: the direct expression, plain control flow, a named function instead of inline route logic. (readable_code: 7, 6)

Rules a script could check, and how often the owner still had to say them: no dict[str, Any] (28), name form such as verb-first, _id, is_* (24), a comment or docstring at most one short sentence (16), a banned or coined word (15), a missing annotation or non-exhaustive match (9), a path in a service or wording in a non-web layer (9), an unread helper or field left after a restructure (7).

When two rules fit one hunk, the owner's comment usually names the more concrete one: the name before the layer, the dict before the type, the deletion before the design.

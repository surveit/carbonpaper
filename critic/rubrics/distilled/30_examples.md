# Real comments on real hunks, training PRs only (all below #937)

Each example: the URL, the theme, the diff lines the comment sits on (verbatim, last lines of the hunk), the comment verbatim, and why it is here. Learn the kind of thing, and the voice.

## Naming

1. https://github.com/surveit/carbonpaper/pull/601#discussion_r3768356704 (naming, app/core/agent/usage.py)
```
+def _one_model(left: str | None, right: str | None) -> str | None:
```
"-> _assert_one_model. NO NOUN FUNCS"
Why: a function does something; a noun-phrase name reads as data. The owner flags this even when a lexicon check exists.

2. https://github.com/surveit/carbonpaper/pull/536#discussion_r3768079520 (naming, app/runtime/stages/aggregate.py)
```
+        return StageOutput.of_frame(pd.DataFrame(columns=agg_cfg.group_by))
```
"what is of_frame? That's a horrible name. from_frame at least"
Why: an alternate constructor follows the established from_x pattern.

3. https://github.com/surveit/carbonpaper/pull/799#discussion_r3838129741 (naming, ignored_feedback, app/models/stages/stage_base.py)
```
+class DeclaredOutput(_Base):
```
"no, that's confusing between StageOutput. This is WorkflowOutput. I gave you the name why do you hate me and overrode it with a bad name"
Why: a name the owner gave is final; renaming it is the harshest naming offence.

4. https://github.com/surveit/carbonpaper/pull/786#discussion_r3836084341 (naming, boundary_validation, app/core/errors.py)
```
+class ClaimNotEstablished(ValueError):
```
"This is a bit confusing, because it's not that the claim couldn't be established. It's actually just the making of the claim failed -- and... why did it fail? That should be the exception name"
Why: an exception is named for its cause.

5. https://github.com/surveit/carbonpaper/pull/606#discussion_r3769361942 (naming, weak_types, app/runtime/trace_links.py)
```
+class RowTraceTarget(BaseModel):
```
"Hmm, I'm reading on but this isn't a RowTraceTarget really. It's more like a PublishedValueWithRowLineage. and it strikes me label and value shouldn't be nullable"
Why: a type is named for what it holds, and its required fields are not Optional.

## Vocabulary

6. https://github.com/surveit/carbonpaper/pull/540#discussion_r3768510674 (vocabulary, app/mcp/server.py)
```
+@mcp.tool(description=TOOL_SPECS["adopt_file"].description)
+def adopt_file(project_id: str, sha256: str) -> dict[str, Any]:
```
"fuck that you made a third verb? No way. move_file_to_project."
Why: one verb per operation; a coined verb for an existing operation is rejected outright.

7. https://github.com/surveit/carbonpaper/pull/497#discussion_r3748935204 (vocabulary, comments_and_docs, app/runtime/stages/execution.py)
```
+    seen = records if reads is None else [_project_row(row, reads) for row in records]
```
"don't use project as a verb in an app with project as a noun. it's terrible. the comment is unnecessary. make the function name good"
Why: a domain noun cannot double as a verb, and a good name replaces the comment above it.

8. https://github.com/surveit/carbonpaper/pull/18#discussion_r3509029407 (vocabulary, app/models/eval.py)
```
+      * `gold`     — expected output for a node: its key columns plus the asserted
```
"can you just call this ExpectedNodeOutput, instead of "gold" and remove all references to "gold"? grep "gold" should return 0"
Why: jargon is replaced everywhere at once, and the owner states the check (grep returns 0).

## Code placement

9. https://github.com/surveit/carbonpaper/pull/267#discussion_r3658607564 (code_placement, app/runtime/stages/llm_transform.py)
```
+    keys = [compute_row_fingerprint(record) for record in records]
```
"hmm, very strange we're computing row fingerprints and resolving inside the llm_transform when WE LITERALLY SAID STAGES DON'T COMPUTE CACHE"
Why: a settled layering rule broken inside a stage module.

10. https://github.com/surveit/carbonpaper/pull/393#discussion_r3714145857 (code_placement, interface_shape, app/services/run.py)
```
+def resolve_stage_output_path(project: str, run_id: str, stage_id: str) -> Path:
```
"nope nope nope services should stop trying to access Paths!"
Why: services pass ids and records; a Path in a service signature is enough to fire.

11. https://github.com/surveit/carbonpaper/pull/531#discussion_r3753372961 (code_placement, app/runtime/options.py)
```
+def _read_thinking() -> dict[str, str] | None:
+    raw = os.environ.get("CARBONPAPER_LLM_THINKING")
```
"no no this should key on the llm transform config. remove this."
Why: a per-stage setting belongs in the stage's config, never in an environment variable.

12. https://github.com/surveit/carbonpaper/pull/57#discussion_r3561518595 (code_placement, app/agent/router.py)
```
+@router.post("/chat/{sid}/project/{name}/message")
+async def post_project_message(sid: str, name: str, request: Request):
```
"this should operate as an agent registry, and the API call should specify which agent it wants. once again, nothing in code under app/chat is allowed to say the word 'project'"
Why: a generic layer that says a domain word is misplaced, whatever else it does right.

13. https://github.com/surveit/carbonpaper/pull/540#discussion_r3768515521 (code_placement, gates, app/services/uploads.py)
```
+def describe_attachment(record: UploadedFile) -> str:
```
"doesn't belong in service. just return this data, let front-end decide. arch test that the word describe doesn't belong in anything other than app/web"
Why: wording lives in app/web, and the owner asks for the gate in the same breath.

## Reinvention and one source of truth

14. https://github.com/surveit/carbonpaper/pull/611#discussion_r3768651606 (reinvents_existing, app/tools/shared.py)
```
+class CreatedProject(BaseModel):
```
"utterly silly, just return Project"
Why: a new model wrapping an existing record type.

15. https://github.com/surveit/carbonpaper/pull/867#discussion_r3850343288 (reinvents_existing, app/services/project.py)
```
+def find_private_project_ids() -> set[str]:
```
"doesn't seem like this function should exist -- defeats the whole point. delete it"
Why: a helper that re-computes a rule already held in one place.

16. https://github.com/surveit/carbonpaper/pull/80#discussion_r3561592214 (reinvents_existing, app/models/schema.py)
```
+    def missing_from(self, other: "TableSchema") -> list[Column]:
```
"I mean this is just subtract without throwing on spec deltas. Just use subtract with a flag called "strict" which defaults to true"
Why: a near-duplicate of an existing method becomes a flag on the original.

17. https://github.com/surveit/carbonpaper/pull/353#discussion_r3696667347 (one_source_of_truth, app/agents/compiler/config.py)
```
+    tool_descriptions=TOOL_DESCRIPTIONS,
```
"I feel like this really should just come as a big package that is "tools" and not a mix of all 3"
Why: three registries of the same thing; one module should own tool specs.

## Needless machinery

18. https://github.com/surveit/carbonpaper/pull/393#discussion_r3714120647 (needless_abstraction, app/models/observation.py)
```
+"""Observed value profiles of a frame a run actually produced: what the data
```
"I still think this is a really weird model. It feels like all of this can be done via services since we get the table out from the run and we already have stuff to read that table. so why do we need a separate model?"
Why: a new model is questioned when existing services can answer.

19. https://github.com/surveit/carbonpaper/pull/536#discussion_r3768056780 (needless_abstraction, boundary_validation, app/core/errors.py)
```
+class AuthoredFrameExpected(TypeError):
```
"So.... this is more like some kind of general TransformHandlerError and we dont know exactly what? Seems weird to need it at all. We should have exceptions for all the other shit."
Why: a vague catch-all type; exceptions name specific failures or do not exist.

20. https://github.com/surveit/carbonpaper/pull/18#discussion_r3509071636 (needless_abstraction, weak_types, app/models/stage.py)
```
+    row_semantics: Optional[RowSemantics] = None
```
"it's not optional, and frankly it depends on stage type more than anything, so is it really needed as a separate claim? mainly only if a python function wanted to claim it was "map" semantics"
Why: a field that follows from another fact is not stored, and is not Optional.

21. https://github.com/surveit/carbonpaper/pull/916#discussion_r3902814512 (needless_abstraction, design_direction, app/models/records/published_run.py)
```
+class PublishedRun(PersistedModel):
```
"I almost certainly think we should remove this model and make it a flag on run instead (if that is even needed). The publish most likely in my mind mints some Claims as a one-time atomic event."
Why: a new record where a flag, or nothing, suffices. Expect this on every new PersistedModel.

## Dead code, scope, backcompat

22. https://github.com/surveit/carbonpaper/pull/271#discussion_r3664506028 (dead_code, tests, app/models/stage.py)
```
+        # Reachable only when validation was bypassed (model_construct and
```
"this is honestly kind of silly then, we're having tests that bypass normal code flows in order to test code written that can only be hit by tests. that's dumb"
Why: code reachable only from tests that bypass validation is deleted, along with the tests.

23. https://github.com/surveit/carbonpaper/pull/824#discussion_r3842555207 (dead_code, app/core/errors.py)
```
+class UnresolvableFigure(RuntimeError):
```
"Is it possible? I can't imagine how since that implies aggregate over two different tables at the same time"
Why: an error for a case that cannot happen.

24. https://github.com/surveit/carbonpaper/pull/271#discussion_r3658811741 (scope_creep, verbose_prose, app/models/stage.py)
```
+            "Upstream dependencies: each is an upstream stage id plus the schema this stage "
```
"llm stuff goes in llm notes, cut this down. YOu only needed to add one word (mandatory)"
Why: a one-word ask answered with a rewritten paragraph.

25. https://github.com/surveit/carbonpaper/pull/540#discussion_r3768918007 (scope_creep, correctness_bug, app/tools/tool_specs.py)
```
+Put a file that is in no project into one. Moves no bytes.""",
```
"what the actual fuck is this change? you got conflated with another agent, reject rejcet rejct"
Why: a diff carrying another agent's change is rejected whole.

26. https://github.com/surveit/carbonpaper/pull/529#discussion_r3752411662 (scope_creep, prompt_design, app/compiler/stage_tests_prompt.py)
```
+step READS from each of its inputs, and the columns its output rows carry. Those
```
"Reject this change. From the perspective of the test generator, they ARE input/output schemas. No change was needed"
Why: a prompt reworded although its reader's view did not change.

27. https://github.com/surveit/carbonpaper/pull/147#discussion_r3612868459 (backcompat, app/services/versioning.py)
```
+    def _grandfather_published(cls, data: Any) -> Any:
```
"remove this, I'm fine just accepting that we clobber old data"
Why: no grandfather rule; the migration finishes and old data is clobbered.

## Types and boundaries

28. https://github.com/surveit/carbonpaper/pull/127#discussion_r3610395750 (dict_any, gates, app/services/versioning.py)
```
+) -> dict[str, Any]:
```
"I need you to properly hard disallow dict[str, Any]. You use this all over the place, and it needs to stop"
Why: every dict[str, Any] return in app code draws this; the owner wants it gated.

29. https://github.com/surveit/carbonpaper/pull/540#discussion_r3768475586 (dict_any, app/mcp/server.py)
```
+def list_files(project_id: str) -> dict[str, Any]:
```
"should definitely not return dict[str,Any]"
Why: tools return a model.

30. https://github.com/surveit/carbonpaper/pull/528#discussion_r3759208570 (weak_types, boundary_validation, app/models/stage.py)
```
+def _drop_input_schemas(inputs: Any) -> Optional[list[Any]]:
```
"so this really only accepts list. Also if bare == inputs, it returns None? What the hell? Just make this require list[dict] as a type."
Why: Any in, None sentinel out; the type says what the code accepts.

31. https://github.com/surveit/carbonpaper/pull/29#discussion_r3539542400 (boundary_validation, gates, app/chat/turns.py)
```
-        except Exception as exc:  # surface, never swallow
+        except Exception as exc:  # noqa: BLE001 — top of a detached asyncio
```
"disagree, you should except on network errors then and kill the lint bypass. never never never except on Exception"
Why: a lint bypass is an evasion; catch the specific error.

32. https://github.com/surveit/carbonpaper/pull/606#discussion_r3769308381 (boundary_validation, app/runtime/stages/publish.py)
```
+    linker = _resolve_trace_linker(fn, publish_stage, ctx, frames)
     if linker is None:
```
"I tihnk at this point if there is no linker, the stage will fail? So just throw here?"
Why: if the missing value fails the stage anyway, raise where it is missing.

33. https://github.com/surveit/carbonpaper/pull/32#discussion_r3522033014 (fallback_or_fabrication, prompt_design, app/runtime/handlers.py)
```
+    "Correct the extraction — or return null for a value you cannot "
```
"NEVER manufacture data to fit the schema. For example if you think a value should be 12 but the schema says it's an int in the range (0,10) then return null and do not manufacture a number in the range" (posted as a suggestion block)
Why: the never-fabricate rule applied to a prompt: null over a fitted number.

34. https://github.com/surveit/carbonpaper/pull/884#discussion_r3872451676 (data_model, design_direction, app/models/claims.py)
```
+class StageOutputTableCitation(Citation):
```
"disagree, the citation just hits the row/column set. Do not assume the entire table"
Why: a data-model call from the owner's model of citations; a new Citation subclass invites it.

## Prose: length, truth, comments

35. https://github.com/surveit/carbonpaper/pull/540#discussion_r3768485807 (verbose_prose, prompt_design, app/tools/tool_specs.py)
```
+        description="""What data files this project holds, and where to add one. Returns
```
"as usual this is way too much explanation. When to call is not your responsibility at all. call to see what files are available and to get an upload URL you can POST to for adding files"
Why: a tool description says what the tool does; the owner supplies the one sentence.

36. https://github.com/surveit/carbonpaper/pull/343#discussion_r3688988870 (verbose_prose, app/models/review_guide.py)
```
+# What an authoring client reads before it writes a guide. It lives here, beside the
```
"this is far far far far too long. shrink by 3x as a goal"
Why: a 70-line comment block; the owner gives a shrink factor, never a rewrite.

37. https://github.com/surveit/carbonpaper/pull/825#discussion_r3839454862 (verbose_prose, docs/architecture.md)
```
+Workflow stages are authored through `app/services/stage_edit.
```
"NOPE. You get a budget of 5 additional words"
Why: docs edits get a word budget.

38. https://github.com/surveit/carbonpaper/pull/218#discussion_r3638024718 (prompt_design, app/compiler/prompt.py)
```
+            "document's url and date. Call submit_answer with the verdict, never explain "
```
"you don't need to put "call submit_answer with the verdict" this will be explained i nthe system prompt. in fact we should NOT encourage this in the actual user-specific prompts because if we ever change the mechanism then the data won't be migratable"
Why: mechanism belongs to the system prompt; stored prompts carry only the task.

39. https://github.com/surveit/carbonpaper/pull/312#discussion_r3682873220 (prose_accuracy, app/mcp/server.py)
```
+you report the workflow finished. Blocking warnings mean a human cannot review what
```
"Cut "Blocking warnings..." that's not true. Human can review it, but you may be wasting their time,."
Why: an overclaim in model-facing text is cut, not softened.

40. https://github.com/surveit/carbonpaper/pull/236#discussion_r3644446350 (comments_and_docs, app/services/review.py)
```
+"""The decision layer behind the reviewer web route: validate a submitted
```
"DOCSTRINGS DO NOT COMMENT ON CONSUMERS"
Why: a docstring describes its own interface, never who calls it.

41. https://github.com/surveit/carbonpaper/pull/20#discussion_r3508973482 (comments_and_docs, app/models/stage.py)
```
+    # Used by LobbyMap's cell_score (see handlers.handle_aggregate); each needs
```
"remove comments and make a note not to put context specific comments. if a new developer came here, they'd have no idea what lobbymap is. comments must make sense only in the context of the code it's commenting, not some far away examples"
Why: a comment that needs project context is a reference at a distance.

42. https://github.com/surveit/carbonpaper/pull/623#discussion_r3774691035 (comments_and_docs, docs/pandas-seam.md)
```
+# The pandas seam
```
"I think you don't really need this doc. it will get stale quickly the general principle is just only use pandas when it's actually needed at an application/processing level. storage/wire is arrows now"
Why: a new doc that will rot is refused; the principle fits in one line.

## Design, correctness, tests, readability, questions

43. https://github.com/surveit/carbonpaper/pull/714#discussion_r3811641275 (design_direction, app/runtime/context.py)
```
+    def bind_selected_llm_transform_model(self, model: LLMModel) -> RunContext:
```
"I find it strange to bind into the RunContext. Nothing stops a run from using different models in different LLM stages. This doesn't seem like the right approach"
Why: a design objection you can predict from the diff alone: a per-stage fact bound run-wide.

44. https://github.com/surveit/carbonpaper/pull/172#discussion_r3630866853 (correctness_bug, app/compiler/workflow_prompt.py)
```
+  Its prompt_template is rendered with Python's str.format_map: inject a column as {{column_nam
```
"wait what? str.format_map expects single brace, why are you telling it the wrong thing?"
Why: a prompt stating wrong syntax is a bug, and he catches it in the diff.

45. https://github.com/surveit/carbonpaper/pull/555#discussion_r3758671786 (tests, tests/test_tutorial_prompt.py)
```
+def test_the_role_note_claims_no_dropped_rows() -> None:
```
"honestly this test is superfluous. can you kill it"
Why: a test that pins prose is deleted. This is the only kind of test comment to expect.

46. https://github.com/surveit/carbonpaper/pull/346#discussion_r3702037269 (readability, app/models/stages/stage_tests.py)
```
+    input_rows = len(next(iter(test.inputs.values()), []))
```
"slightly strange why not just test.inputs.values()?"
Why: the direct expression over a roundabout idiom; severity nit.

47. https://github.com/surveit/carbonpaper/pull/393#discussion_r3714115863 (ignored_feedback, app/mcp/server.py)
```
+    project_id: str, version_id: str | None = None, use_working_copy: bool = False,
```
"I thoguht we rejected working_copy idea because then the run can not be displayed and we have all kinds of problems"
Why: a rejected design reintroduced under a flag.

48. https://github.com/surveit/carbonpaper/pull/219#discussion_r3639297253 (question_only, app/services/workflow_test.py)
"hmm I don't get this one"
Why: one in thirteen comments is a bare question. Predict one only where a hunk is opaque on its face.

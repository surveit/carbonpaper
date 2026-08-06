# carbonpaper — reviewable AI workflows

**Workflows of typed, schema-validated stages** with human-review gates and persisted
runs. Vocabulary (locked): **project** = the container
directory · **methodology** = the authored prose (`methodology_raw.md`) ·
**workflow** = the stage graph it compiles to. What/why + features → `docs/overview.md`;
code map → `docs/architecture.md`; quickstart → `README.md`.

## The 12 stage types
`input_data` · `llm_transform` · `python_row_function` · `python_frame_function` ·
`starlark_row_function` · `enrich` · `expand` · `aggregate` · `human_review_queue` ·
`publish` · `union` · `filter_rows`. Prefer `python_row_function` (runtime-enforced 1:1)
unless the logic needs the whole frame; prefer `starlark_row_function` over
`python_row_function` when the code should be sandboxed (no import, file, or network
access). `enrich` and `expand` are both LEFT joins of a reference input into a subject
input, differing only in permitted cardinality (m:1, verified; vs m:n fan-out); neither
drops a subject row — that is `filter_rows`' job.

## Repo layout
```
app/models/    stage-type schemas (Pydantic) — source of truth; loader rejects invalid workflows
app/runtime/   the Runner (executor, stages/, LLM backends)   → app/runtime/AGENTS.md
app/compiler/  prose → LLM generation engines (data model, stage tests)
app/web/       FastAPI routers + diagrams (thin app/main.py)  → app/AGENTS.md
app/services/  web-independent logic (loader, generation, node review, versioning, drafts)
app/chat/  PydanticAI chat · app/core/llm/  model menu · tests/  pytest (offline)
```

## Conventions (load-bearing)
- **Never fabricate.** An unsourceable value is `null`/`unknown`; the pipeline fails loudly
  rather than inventing a number, URL, or citation. A requested LLM backend that isn't
  available raises rather than silently substituting another.
- **`app/services` never imports `app/web`; `{project,node_review,versioning,drafts}` never import
  `app.main`/`app.runtime`/`app.compiler`.** Services sit below the routes and the agent tools —
  never the reverse (the generation step that runs a compiler agent lives in
  `app.services.generation`, which wraps the compiler). Both enforced by import-linter.
- **Never `except Exception` or bare `except`.** Catch specific types — swallowing errors breaks
  fail-loudly. Enforced by Ruff `BLE001`.
- **A system prompt states the model's ROLE in the wider system, and what becomes of its
  output.** Not just the task: who reads the result, what it is shown beside, what the reader
  is deciding, and what the model will *not* be told (e.g. the stage-test generator never sees the
  code or the pass/fail). A model given only a task optimises the artifact; one given its place
  optimises the reader's decision, and the two differ — ordering worked examples so they
  *explain* rather than merely cover does not follow from the task alone. Include at least one
  worked example of the output.
- **No `dict[str, Any]` as a stand-in for a structured value.** A dict with a known, fixed set of
  keys is a missing model — define a Pydantic model (`PersistedModel` for a stored object) or
  reference an existing one, and pass *that*. A function returning `dict[str, Any]`, or a field
  typed `dict[str, Any]`, for something with named fields is a review-blocking smell — model it.
  `dict[str, Any]` is allowed only at a genuine dynamic-JSON boundary where the shape is
  caller-defined and not yet known: a raw stage-spec dict that may be invalid mid-edit (matching
  `stage_to_spec_dict` / `validate_workflow_draft`), or foreign JSON being parsed — and even
  there, parse into a model at the first point the shape is known.
- **Banned words are enforced, not advisory.** `tests/arch/test_no_banned_words.py` fails on any
  word in its `BANNED_WORDS` set across `.py`/`.md`/`.html`/`.js`/`.css` — read that set for the
  current list and the replacement each word owes you. The test file is the only place a banned
  word may appear, so name the specific property instead: a sorted-key JSON dump, the spec-dict
  form, the on-disk text, `HASH_IGNORED_KEYS`.
- **No `__all__`.** Nothing here star-imports, so it is only a second registry of public names
  to keep in sync. A package hub re-exports with the redundant-alias form
  `from x import y as y`, which Ruff and mypy both read as explicit. Enforced by
  `tests/arch/test_no_dunder_all.py`, with an empty allowlist.
- **Never weaken an arch test without human approval.** The import-linter contracts
  (`pyproject.toml`, run as `lint-imports`) and the AST invariant tests (`_arch_tests/`,
  `tests/arch/`) exist to fail on work in progress — that failure is the signal, not an obstacle.
  Adding a contract or adding to a test is fine; relaxing, deleting, skipping, or adding an
  allowlist entry to an existing one is a human decision on the record. When an arch test blocks
  the change you were about to make, reroute the change and say in the PR which test caught you
  and what you did differently.
- **A module at the size ceiling gets split, not squeezed.** The file-size ratchet
  (`tests/arch/test_file_size_ratchet.py`) fails a file in `app/` over the LLOC ceiling, and the
  only sanctioned way back under is moving a cohesive group of code out to its own module. Never
  buy the statements back by writing denser code — fusing named steps into one long expression,
  dropping an intermediate variable that was carrying a name, inlining a small helper into its
  caller. That spends the readability the ceiling exists to protect and leaves the next change
  with even less room. If the split is bigger than the change you are on, say so in the PR and
  let a human decide whether to take it now.
- **On a stored model, only ADDING an OPTIONAL field is safe.** `PersistedModel.load` is a
  strict `model_validate` with `extra="forbid"`, so a record written last week is parsed by
  today's model with no leniency, and both directions break it: adding a REQUIRED field
  orphans every record that lacks it, and REMOVING a field orphans every record that still
  carries it. This reaches through nesting — a `WorkflowVersion` embeds whole `Stage`s, so
  `QueueConfig` and `JoinConfig` are as load-bearing as the record class. All three have
  happened: `queue`'s column names and `join.enrich_with` arrived as required, and `guide`
  left `WorkflowVersion` for its own record. A read failure does not stay local either —
  `_build_project_card` loads every project's versions, so one unreadable document takes
  down the home page for every project. Changing a stored shape means writing an Alembic
  revision (`alembic/versions/`, `alembic upgrade head`) that rewrites the affected JSON
  payloads; a revision may refuse a record it cannot determine, and MUST refuse rather than
  fill a value the stored data does not carry. **No test enforces the shape rule yet**, so
  it is a review-time rule: raise it in review rather than assuming CI will.
- **Planning docs stay out of the repo.** Design specs, implementation/execution plans,
  brainstorming or "rethink" notes, and refactor/migration roadmaps are ephemeral working
  artifacts — keep them in scratch or the PR description, never commit them. Committed docs
  describe what the code does *today* (reference docs like `docs/architecture.md`), not what we
  plan to do.

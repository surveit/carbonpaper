# glassbox — reviewable AI workflows

**Workflows of typed, schema-validated stages** with human-review gates and persisted
runs. Vocabulary (locked): **project** = the container
directory · **methodology** = the authored prose (`methodology_raw.md`) ·
**workflow** = the stage graph it compiles to. What/why + features → `docs/overview.md`;
code map → `docs/architecture.md`; quickstart → `README.md`.

## The 8 stage types
`input_data` · `llm_transform` · `python_row_function` · `python_frame_function` · `join` ·
`aggregate` · `human_review_queue` · `publish`. Prefer `python_row_function` (runtime-enforced
1:1) unless the logic needs the whole frame.

## Repo layout
```
app/models/    stage-type schemas (Pydantic) — source of truth; loader rejects invalid workflows
app/runtime/   the Runner (executor, stages/, LLM backends)   → app/runtime/AGENTS.md
app/compiler/  prose → LLM → workflow engine (python -m app.compiler)
app/web/       FastAPI routers + diagrams (thin app/main.py)  → app/AGENTS.md
app/services/  web-independent logic (loader, compilation, node review, versioning, drafts)
app/chat/  PydanticAI chat · app/core/llm/  model menu · tests/  pytest (offline)
```

## Conventions (load-bearing)
- **Never fabricate.** An unsourceable value is `null`/`unknown`; the pipeline fails loudly
  rather than inventing a number, URL, or citation. A requested LLM backend that isn't
  available raises rather than silently substituting another.
- **`app/services` never imports `app/web`; `{project,node_review,versioning,drafts}` never import
  `app.main`/`app.runtime`/`app.compiler`.** Services sit below the routes and the agent tools —
  never the reverse (the compile step a workflow regenerate needs lives in
  `app.services.compilation`, which wraps the compiler). Both enforced by import-linter.
- **Never `except Exception` or bare `except`.** Catch specific types — swallowing errors breaks
  fail-loudly. Enforced by Ruff `BLE001`.
- **No `dict[str, Any]` as a stand-in for a structured value.** A dict with a known, fixed set of
  keys is a missing model — define a Pydantic model (`PersistedModel` for a stored object) or
  reference an existing one, and pass *that*. A function returning `dict[str, Any]`, or a field
  typed `dict[str, Any]`, for something with named fields is a review-blocking smell — model it.
  `dict[str, Any]` is allowed only at a genuine dynamic-JSON boundary where the shape is
  caller-defined and not yet known: a raw stage-spec dict that may be invalid mid-edit (matching
  `stage_to_spec_dict` / `validate_workflow_draft`), or foreign JSON being parsed — and even
  there, parse into a model at the first point the shape is known.
- **Planning docs stay out of the repo.** Design specs, implementation/execution plans,
  brainstorming or "rethink" notes, and refactor/migration roadmaps are ephemeral working
  artifacts — keep them in scratch or the PR description, never commit them. Committed docs
  describe what the code does *today* (reference docs like `docs/architecture.md`), not what we
  plan to do.

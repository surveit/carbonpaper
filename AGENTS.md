# prototype_one — reviewable AI workflows

**Workflows of typed, schema-validated stages** with human-review gates and persisted
runs. Vocabulary (locked; `docs/naming-refactor.md`): **project** = the container
directory · **methodology** = the authored prose (`methodology_raw.md`) ·
**workflow** = the stage graph it compiles to. What/why + features → `docs/overview.md`;
code map → `docs/architecture.md`; quickstart → `README.md`.

## The 8 stage types
`input_data` · `llm_transform` · `python_row_function` · `python_frame_function` · `join` ·
`aggregate` · `human_review_queue` · `publish`. Prefer `python_row_function` (runtime-enforced
1:1) unless the logic needs the whole frame.

## Repo layout
```
app/core/models/    stage-type schemas (Pydantic) — source of truth; loader rejects invalid workflows
app/runtime/   the Runner (executor, stages/, LLM backends)   → app/runtime/AGENTS.md
app/compiler/  prose → LLM → workflow engine (python -m app.compiler)
app/web/       FastAPI routers + diagrams (thin app/main.py)  → app/AGENTS.md
app/services/  web-independent logic (loader, compilation, node review, versioning)
app/chat/  PydanticAI chat · app/core/llm/  model menu · tests/  pytest (offline)
```

## Conventions (load-bearing)
- **Never fabricate.** An unsourceable value is `null`/`unknown`; the pipeline fails loudly
  rather than inventing a number, URL, or citation. A requested LLM backend that isn't
  available raises rather than silently substituting another.
- **`app/services` never imports `app/web`; `{project,node_review,versioning}` never import
  `app.main`/`app.runtime`/`app.compiler`.** Services sit below the routes and the agent tools —
  never the reverse (the compile step a workflow regenerate needs lives in
  `app.services.compilation`, which wraps the compiler). Both enforced by import-linter.
- **Never `except Exception` or bare `except`.** Catch specific types — swallowing errors breaks
  fail-loudly. Enforced by Ruff `BLE001`.

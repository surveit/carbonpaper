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
app/models/    stage-type schemas (Pydantic) — source of truth; loader rejects invalid workflows
app/runtime/   the Runner (executor, stages/, LLM backends)   → app/runtime/AGENTS.md
app/compiler/  prose → LLM → workflow engine (python -m app.compiler)
app/web/       FastAPI routers + diagrams (thin app/main.py)  → app/AGENTS.md
app/services/  web-independent logic (loader, compilation, node review, versioning)
app/chat/  PydanticAI chat · app/llm/  model menu · tests/  pytest (offline)
```

## Conventions (load-bearing)
- **Never fabricate.** An unsourceable value is `null`/`unknown`; the pipeline fails loudly
  rather than inventing a number, URL, or citation. A requested LLM backend that isn't
  available raises rather than silently substituting another.
- **Schemas are called schemas** — `output_schema`, an input `schema:` block, a `TableSchema`.
  Never "contract"; the word adds no meaning and splits one concept across two names.
- **`human_review_queue` handles asymmetrical risk.** A wrong result that's expensive or
  irreversible halts for sign-off; decisions are content-hashed to survive re-runs.
- **`app/services/{project,node_review,versioning}` stay below the routes layer** — no importing
  `app.main`/`app.runtime`/`app.compiler` (import graph stays acyclic). Not lint-enforced: #63.
- **Never `except Exception` or bare `except`.** Catch specific types — swallowing errors breaks
  fail-loudly. Enforced by Ruff `BLE001`.

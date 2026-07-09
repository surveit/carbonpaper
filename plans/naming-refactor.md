# Naming refactor: project / methodology / workflow (retire DAG)

> **`/plans` scratch cache — historical, not authoritative.** This refactor has
> **already merged** (the codebase uses `project` / `methodology` / `workflow`
> and `app/models/workflow.py` throughout). Kept here only as the record of why
> those names were chosen and where "DAG" went — do not follow the execution
> checklist below as a live plan, and do not cite this file for how the code
> works today. For that, read `docs/architecture.md` or the code itself.

**Status:** MERGED. Vocabulary was APPROVED 2026-07-04 and executed as a
focused refactor. This is the "giant" one — ~500 sites.

---

## Vocabulary

| term | meaning | was |
|---|---|---|
| **project** | the **container / workspace** — one repeatable accountability pipeline. Holds a methodology, a workflow, a data model, evals, versions, and runs. The top-level unit a user opens. | "methodology" in most of today's code (the dir, the route, the workspace). Reinstates the previously-banned word as *the* container. |
| **methodology** | the **authored prose** — the human-written method/approach that compiles into the workflow. The "why / how", in words. | the "Document" concept / the compiler's prose input; largely unnamed in code today. |
| **workflow** | the **executable stages** — the compiled graph that runs. | the `Methodology` pydantic model (a list of stages), and every "DAG". |
| **version** | immutable snapshot of a workflow (+ schemas); runs pin to one. | unchanged. |
| **run** | one execution of a version. | unchanged. |
| **stage** / **node** | a step's spec / its box in the workflow view (1:1). | unchanged. |
| ~~DAG~~ | **BANNED** → "workflow" (or "workflow graph" where the graph *structure* is specifically meant). | 115 occurrences. |

Each word now sits in its most natural sense — project = obviously a container,
methodology = the written method (matches "here's our methodology"), workflow =
the pipeline of steps. None are synonyms of each other.

## Why this is bigger than it first looks: "methodology" splits THREE ways

Today's `methodology` is overloaded across three of the new concepts. Every
occurrence must be routed, not globally replaced:

1. **container sense** (the dir, `/methodology/{name}` routes, workspace,
   `methodology_dir`, `EXAMPLES_DIR / methodology`) → **project** — the bulk,
   ~340 of the 382.
2. **model sense** (`class Methodology` = a list of stages, and its helpers) →
   **workflow**.
3. **the prose method doc** (currently the "Document" / compiler input, mostly
   unnamed in code) → *gains* the freed word **methodology**.

Plus every **DAG** (115) → **workflow**.

So unlike a container-keeps-its-name plan, here nearly everything moves: container
→ project, model → workflow, DAG → workflow, and the prose picks up methodology.

## Rename inventory

### Container sense → `project`
- `methodology_dir` → `project_dir`
- `EXAMPLES_DIR / methodology` (and the `methodology` param/var) → `project`
- `/methodology/{methodology}` routes → `/project/{project}`
- `list_methodologies` → `list_projects`
- UI: "the methodology page", index cards, sidebar → "project"
- **Example directories stay as-is.** `examples/congresswatch/` etc. are project
  *names* — "congresswatch" is a project. No directory rename needed; only the
  *concept* label changes. (This de-risks the whole refactor — no committed
  run/version paths move.)

### Model sense → `workflow`
- `class Methodology` (`app/models/methodology.py`) → `class Workflow`
  (`app/models/workflow.py`)
- `validate_methodology_stages(list[Stage])` → `validate_workflow(list[Stage])`
  *(the multi-error fix — checks return all findings — is being done ahead of
  this refactor in the #30 smaller-fixes pass; keep the new behavior)*
- `validate_methodology(list[dict])` (test-only) → drop; callers use
  `parse_workflow` or the typed `validate_workflow`
- `parse_methodology` → `parse_workflow`
- `load_methodology_stages` → `load_workflow`
- `MethodologyLoadError` → `WorkflowLoadError`
- `Methodology._validate_dag` → `Workflow._validate_graph`
- `app/models/__init__.py` exports → update

### `DAG` → `workflow` (115)
- Prose/comments/UI/identifiers. Use "workflow graph" only where the graph
  *structure* is the point. In `detect_cycle` / `topological_sort`, keep a local
  comment noting the graph is/must be acyclic, so that invariant isn't lost with
  the word.

### The prose doc → `methodology`
- The "Document" tab/route and the compiler's prose input take the name
  **methodology** — the written method that compiles to the workflow. (The shell
  that surfaces this may live on another branch; apply where it exists.)

### Feature rename
- "reviewable workflow" (the per-node belief-approval feature) → **node review**
  (the code's own term), so "workflow" stays a clean noun for the stage graph.

## Open decisions (small)
- **Route paths `/methodology/*` → `/project/*`?** Recommend yes for consistency;
  it's a user-facing URL change, so confirm before the sweep.
- **`load_methodology_stages` arg name:** the function returns a workflow but
  takes a project dir → `load_workflow(project_dir)`. Keep the arg named
  `project_dir`.

## Execution order (as planned — completed; kept for the historical trail)
1. Model sense: `methodology.py` → `workflow.py`; `Methodology` → `Workflow`;
   validators/`parse_*`; `__init__` exports.
2. Loader: `load_methodology_stages` → `load_workflow`, `MethodologyLoadError`
   → `WorkflowLoadError`; update importers.
3. Container sense → `project`: `methodology_dir`, routes, `list_methodologies`,
   `EXAMPLES_DIR` var, UI labels.
4. `DAG` → `workflow` sweep across `app/**`, `docs/**`, templates. Keep acyclic
   notes.
5. Prose doc → `methodology` (the Document surface, where it exists).
6. "reviewable workflow" → "node review".
7. Verify: no bare `DAG`; container-sense `methodology` gone (only prose-doc
   sense survives); pytest + ruff + mypy green.

(The loader-relocation and version-lifecycle coordination notes that used to
live here described in-flight work on other passes; that work has since landed
— see `docs/architecture.md` for the current shape of `app/services/loader.py`
and `app/services/versioning.py`.)

# Project Editing Agent — Implementation Plan (Plan 1 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a journalist tools to author and refine a project's workflow by chatting with it — read the workflow DAG, edit a stage (validated), snapshot a version, and (re)compile from a source document — mounted as a per-project chat, reusing the existing chat spine.

**Architecture:** The agent is *orchestration over existing services*. Every tool is a plain Python callable that calls an existing service function in-process (never an HTTP route). The tools plug into the **existing PydanticAI `ChatEngine`** (`app/chat/engine.py`) exactly like the current `_list_projects` demo tool. No new engine, no store changes — those are Plan 2.

**Tech Stack:** Python 3, FastAPI, PydanticAI (`ChatEngine` + `Agent`), Pydantic v2 (`app/models`), the loader as the single stage-I/O owner (JSON on disk), pandas (node-decision store), pytest.

**Base:** `master @ 68fd38e` (after PR #47 landed the naming refactor onto the compiler). Final vocab (Project = the container under `examples/<name>/`; Workflow = its DAG of stages; Methodology = the prose the compiler reads). Compiled stages are **JSON**.

---

## Vocabulary (this codebase, post-#47)

- **Project** — one pipeline the journalist authors, a folder `examples/<name>/`. The container.
- **Workflow** — the project's DAG of typed stages, loaded from `examples/<name>/compiled/NN_<id>.json`.
- **Stage** — one node. 7 `StageType`s: `input_data`, `llm_transform`, `python_row_function`, `python_frame_function`, `join`, `aggregate`, `human_review_queue`, `publish`. Each carries an `output_schema`.
- **Methodology** — the *prose* document the compiler turns into a workflow (`compile_methodology`). Not the container.
- **Node review** — a human marking one stage `approved`/`rejected`/`edited_stale`/`unreviewed` (colors the DAG; does not halt a run).
- **Version** — an immutable snapshot of `compiled/` (+ `schemas/`) under `versions/<id>/`.

> The design doc `docs/methodology_editing_agent.md` predates #47 and is written in the old vocab (Methodology/DAG). It remains the design record; **this plan is the current, final-vocab source of truth.**

---

## Scope split (why this is Plan 1 of 2)

Design doc §5 chose an SDK-MCP / claude-agent-sdk-native engine so tools run on the Claude **subscription (no API key)**. That engine does **not** exist yet: `ClaudeCLIModel` (`app/chat/claude_cli_model.py`) drives the subscription backend for plain chat only — `allowed_tools=[]`, no tool loop. Building it is an independent subsystem.

Tools/guards/mounting are backend-agnostic, so:
- **Plan 1 (this file):** tools + per-project mounting on the existing PydanticAI `ChatEngine` (which already runs a tool loop in `stream_turn`'s `is_call_tools_node` path). Runs end-to-end today on the API backend (`AnthropicModel`). Every tool is unit-tested offline against the filesystem/services — no LLM required.
- **Plan 2 (separate, later):** claude-agent-sdk-native engine (SDK-MCP tools on `ClaudeAgentOptions`, CLI tool loop, reuse `app/runtime/llm_agent_sdk.py` mapping) + engine-neutral `store.py`. Same tools, subscription, no API key.

---

## §10 open questions (from the design doc) — resolved against the current code

1. **Does the compiler emit `schemas/`? → No.** `write_methodology` writes only `compiled/NN_<id>.json` + `methodology_raw.md` + `compiler_result.json`. The executable data model is the per-stage `output_schema` (a `TableSchema` on `Stage`), edited via `edit_stage`. A separate hand-authored named-schema library exists (`app/models/named_schemas.py`, gated by `node_review.data_model_state`/`approve_schema_library`) but the compiler does not produce it. → **`edit_data_model` is out of scope for Plan 1**; a data-model agent over the schema library is a later, separate embedding.
2. **`store.py` engine-neutral shape? → Deferred to Plan 2.** Still PydanticAI `ModelMessage`; reused unchanged here.
3. **Where does compile write? → Two flows now exist; the agent uses direct-write.** The web app compiles into a staged, audited `compilations/<id>/` (via `run_compilation`) with an `ok|invalid|error` manifest, and **nothing auto-promotes** that into `examples/<name>/compiled/`. The agent's `compile_workflow` tool instead writes **straight into the live project's `compiled/`** via `write_methodology(result, examples/<name>)` (all stages amber; the review state is the staging), which matches the design doc's §10-Q3 leaning and closes the loop without a promote step. **Documented divergence:** this bypasses the `compilations/` audit trail the UI uses — revisit if the agent should stage-and-promote instead (⚠️ CHECKLIST item).

---

## Global Constraints

- **Never fabricate a spec.** A tool lacking a real value (column, source, path) **raises** with a loud message; never invents a default. No silent fallback.
- **Every edit validates before it writes.** `edit_stage` runs `validate_stage`; on any issue it writes nothing and returns the issues. Mirrors the `node_edit` route exactly.
- **The agent may `create_version`; it may NOT approve nodes.** No tool records an approval. Edits land `edited_stale` (amber).
- **Regenerate snapshots first.** `compile_workflow` over an existing non-fresh `compiled/` must `create_version` first and requires `confirm_overwrite=True`.
- **Doc stays on disk, never in agent context.** `fetch_document` returns a path + outline; `read_section`/`grep_doc` return bounded slices; `compile_workflow` reads the path. No tool returns full doc text.
- **Stage I/O goes through the loader.** Do not `json.load`/`glob` compiled files or call `model_dump_json` directly — use `loader.load_compiled_dir` / `find_stage_file` / `write_stage` / `stage_to_spec_dict` / `stage_to_json`. The loader is the single owner of the on-disk format.
- **Typing:** no `Any` leakage, no `# type: ignore`. New exceptions go in `app/errors.py` (create it — it does not exist on this base; keep it import-free).
- **Naming:** final vocab (project/workflow); full identifier names (no `msg`/`ev`/`sid`).
- **"schema" not "contract"** for `output_schema` in prose/docstrings.
- **TDD, small commits, push each commit to origin** on `methodology-editing-agent`.

---

## File Structure

**Create:**
- `app/services/stage_edit.py` — `edit_stage_spec(...)`: the validated single-stage writer, extracted from the `node_edit` route so route and tool share one writer. (Kept out of `node_review.py`, which is deliberately `app.models`-free.)
- `app/services/workspace.py` — `list_project_names(examples_dir)` and `project_workflow_summary(project_dir)` (read helpers the tools need; none exist yet).
- `app/chat/project_tools.py` — the tool callables via `make_project_tools(name, *, examples_dir)` (closures bound to one project) + cross-project `list_projects`.
- `app/chat/project_agent.py` — `build_project_agent(name)` / `get_project_agent(name)` (a `ChatEngine` with bound tools + system prompt; cached per name).
- `app/errors.py` — `RegenerateWithoutSnapshotError`.
- Tests: `tests/test_stage_edit.py`, `tests/test_workspace.py`, `tests/test_project_tools.py`, `tests/test_project_agent.py`, `tests/test_project_chat_routes.py`.

**Modify:**
- `app/web/routers/node_review.py` — `node_edit` route delegates to `stage_edit.edit_stage_spec` (HTTP contract unchanged; single writer).
- `app/chat/router.py` — mount project-scoped chat routes reusing `TurnManager` + `SessionStore` + `chat.html`.
- `app/templates/project.html` — a link/panel to open the project's editing chat (FE event renderer is already generic — `chat.html:136-141` handles `thinking`/`text`/`tool_call`/`tool_result`).

---

## Interfaces this plan consumes (recon'd on `master @ 68fd38e`, verbatim)

```
# app/services/loader.py — the ONE owner of compiled-stage I/O (JSON)
@dataclass CompiledStageFile: filename: str; stage: Stage | None; issues: list[str]
class WorkflowLoadError(Exception)                                       # .issues: list[str]
load_compiled_dir(compiled_dir: Path) -> list[CompiledStageFile]        # tolerant
load_workflow(project_dir: Path) -> list[Stage]                         # strict, raises WorkflowLoadError
stage_to_spec_dict(stage: Stage) -> dict[str, Any]                      # model_dump(mode="json", by_alias=True, exclude_none=True)
stage_to_json(stage: Stage) -> str
find_stage_file(compiled_dir: Path, stage_id: str) -> Path | None
write_stage(path: Path, stage: Stage) -> None

# app/services/node_review.py  (pure stdlib + json + pandas; no app.models import)
CANONICAL_IGNORE_KEYS = {"_filename", "_order", "_error"}
canonical_node_spec(stage: dict) -> dict
node_content_hash(stage: dict) -> str
load_node_decisions(project_dir: Path) -> pd.DataFrame
record_node_decision(project_dir, *, stage_id, content_hash, decision, reviewer, dag_version=None, note=None, reviewed_at=None) -> pd.DataFrame
approval_state_for(stage: dict, df) -> {"state","current_hash","matched_decision"}   # state ∈ {approved,rejected,unreviewed,edited_stale}

# app/services/versioning.py
create_version(project_dir: Path, *, message: str, reviewer: str, parent_version: str | None = None) -> dict[str, Any]
list_versions(project_dir: Path) -> list[dict]     # newest-first; each has ["id"]

# app/services/compilation.py
write_methodology(result: dict[str, Any], out_dir: str | Path) -> dict   # writes <out>/compiled/NN_<id>.json + methodology_raw.md + compiler_result.json
run_compilation(input_path, name, model="sonnet", compilations_root=COMPILATIONS_ROOT) -> str  # staged flow (NOT used by the agent)

# app/compiler/__init__.py  __all__ = ["read_input", "compile_methodology"]
read_input(path: str | Path) -> str
compile_methodology(input_text: str, name: str, model="sonnet", timeout_s=600, max_attempts=3) -> dict
    # {name, stages, methodology_raw, compiler_notes, validation, prompt, raw_llm}; can raise on bad model output

# app/models
Stage  # id, type: StageType, name, source, inputs: list[InputRef], output_schema: TableSchema|None, one handle per type
validate_stage(stage: dict[str, Any]) -> list[str]    # [] == valid
StageType(str, Enum): input_data, llm_transform, python_row_function, python_frame_function, join(="join"), aggregate, human_review_queue, publish

# node_edit route (the writer this plan extracts) — app/web/routers/node_review.py:131
POST /project/{project}/node/{stage_id}/edit  (spec_text: str = Form(...), JSON)
  parse JSON -> strip CANONICAL_IGNORE_KEYS -> guard id==stage_id -> validate_stage -> find_stage_file -> write_stage(Stage.model_validate(...)) -> hash+state

# app/chat/engine.py
ChatEngine(*, system_prompt: str, tools: list[Callable]|None=None, toolsets: list|None=None, model=None)
async ChatEngine.stream_turn(prompt: str, *, message_history, emit) -> list[ModelMessage]

# app/chat/turns.py / router.py
TurnManager.start(*, engine, store, session_id: str, prompt: str) -> str
_store: SessionStore  (app/chat/router.py module-level);  demo engine wired tools=[_list_projects]

# app/web/config.py
EXAMPLES_DIR = REPO_ROOT / "examples"
```

---

# Phase A — Read tools (no writes)

### Task A1: `list_project_names` + `project_workflow_summary` service helpers

**Files:** Create `app/services/workspace.py`; Test `tests/test_workspace.py`

**Interfaces produced:**
- `list_project_names(examples_dir: Path) -> list[str]` — sorted names of dirs under `examples_dir` containing a `compiled/`.
- `project_workflow_summary(project_dir: Path) -> dict[str, Any]` — `{"name", "stages":[{"id","type","name","inputs":[str],"review_state"}], "issues":[str]}` via the tolerant loader + node_review.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_workspace.py
import json
from pathlib import Path

from app.services import workspace


def _write_stage(compiled: Path, order: int, sid: str, stype: str, inputs: list[str]) -> None:
    compiled.mkdir(parents=True, exist_ok=True)
    stage: dict = {"id": sid, "name": f"{sid} step", "type": stype}
    if inputs:
        stage["inputs"] = [{"id": dep} for dep in inputs]
    (compiled / f"{order:02d}_{sid}.json").write_text(json.dumps(stage), encoding="utf-8")


def test_list_project_names_only_dirs_with_compiled(tmp_path: Path) -> None:
    _write_stage(tmp_path / "alpha" / "compiled", 1, "load", "input_data", [])
    (tmp_path / "not_a_project").mkdir()
    assert workspace.list_project_names(tmp_path) == ["alpha"]


def test_workflow_summary_reports_ids_types_inputs_and_review_state(tmp_path: Path) -> None:
    pdir = tmp_path / "alpha"
    _write_stage(pdir / "compiled", 1, "load", "input_data", [])
    _write_stage(pdir / "compiled", 2, "score", "llm_transform", ["load"])
    summary = workspace.project_workflow_summary(pdir)
    assert summary["name"] == "alpha"
    by_id = {s["id"]: s for s in summary["stages"]}
    assert by_id["score"]["type"] == "llm_transform"
    assert by_id["score"]["inputs"] == ["load"]
    assert by_id["load"]["review_state"] == "unreviewed"
    assert summary["issues"] == []
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_workspace.py -v` → `ModuleNotFoundError: app.services.workspace`

- [ ] **Step 3: Implement `app/services/workspace.py`**

```python
"""workspace.py — read-only helpers for enumerating projects and summarizing one
project's workflow (stage ids/types/inputs + per-node review state). These back
the editing agent's read tools. Uses the tolerant loader (a malformed compiled
file becomes an issue, not an exception) and the node-review store; imports
nothing from the web layer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.services import node_review
from app.services.loader import load_compiled_dir, stage_to_spec_dict


def list_project_names(examples_dir: Path) -> list[str]:
    """Sorted names of every project under `examples_dir` — a directory counts
    only if it contains a `compiled/` subdirectory (an authored workflow)."""
    if not examples_dir.is_dir():
        return []
    return sorted(
        child.name
        for child in examples_dir.iterdir()
        if child.is_dir() and (child / "compiled").is_dir()
    )


def project_workflow_summary(project_dir: Path) -> dict[str, Any]:
    """A compact summary of one project's workflow: each stage's id, type, name,
    upstream input ids, and review state. Never returns full stage specs — that is
    `read_stage`'s job. A single malformed compiled file surfaces in `issues`."""
    compiled = load_compiled_dir(project_dir / "compiled")
    decisions = node_review.load_node_decisions(project_dir)

    stages: list[dict[str, Any]] = []
    issues: list[str] = []
    for compiled_file in compiled:
        if compiled_file.stage is None:
            issues.append(f"{compiled_file.filename}: {'; '.join(compiled_file.issues)}")
            continue
        stage = compiled_file.stage
        spec = stage_to_spec_dict(stage)
        state = node_review.approval_state_for(spec, decisions)["state"]
        stages.append({
            "id": stage.id,
            "type": stage.type.value,
            "name": stage.name,
            "inputs": [ref.id for ref in stage.inputs],
            "review_state": state,
        })
    return {"name": project_dir.name, "stages": stages, "issues": issues}
```

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_workspace.py -v` → PASS (2)

- [ ] **Step 5: Typecheck + commit**
```bash
python -m mypy app/services/workspace.py
git add app/services/workspace.py tests/test_workspace.py
git commit -m "feat(services): workspace read helpers (list projects + workflow summary) for the editing agent"
git push
```

---

### Task A2: Read tools — `list_projects`, `describe_workflow`, `read_stage`

**Files:** Create `app/chat/project_tools.py`; Test `tests/test_project_tools.py`

**Interfaces produced:** `make_project_tools(name: str, *, examples_dir: Path) -> list[Callable]` — closures bound to one project. This task adds three read tools; later tasks extend the same factory. Returns JSON-serializable values.
- `list_projects() -> list[str]`
- `describe_workflow() -> dict` (the workflow summary for the bound project)
- `read_stage(stage_id: str) -> str` (the stage's on-disk JSON; raises `ValueError` if absent)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_project_tools.py
import json
from pathlib import Path

import pytest

from app.chat import project_tools


def _seed(examples: Path, name: str) -> Path:
    compiled = examples / name / "compiled"
    compiled.mkdir(parents=True, exist_ok=True)
    (compiled / "01_load.json").write_text(
        json.dumps({"id": "load", "name": "Load rows", "type": "input_data"}), encoding="utf-8"
    )
    return examples / name


def _tool(tools: list, fn_name: str):
    for tool in tools:
        if tool.__name__ == fn_name:
            return tool
    raise AssertionError(f"tool {fn_name!r} not registered")


def test_read_tools_report_workspace(tmp_path: Path) -> None:
    _seed(tmp_path, "alpha")
    tools = project_tools.make_project_tools("alpha", examples_dir=tmp_path)
    assert _tool(tools, "list_projects")() == ["alpha"]
    assert _tool(tools, "describe_workflow")()["name"] == "alpha"
    assert '"id": "load"' in _tool(tools, "read_stage")("load")


def test_read_stage_missing_fails_loud(tmp_path: Path) -> None:
    _seed(tmp_path, "alpha")
    tools = project_tools.make_project_tools("alpha", examples_dir=tmp_path)
    with pytest.raises(ValueError, match="no stage 'nope'"):
        _tool(tools, "read_stage")("nope")
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_project_tools.py -v` → `ModuleNotFoundError`

- [ ] **Step 3: Implement `app/chat/project_tools.py`**

```python
"""project_tools.py — in-process tools the editing agent calls to read and edit
ONE project's workflow. `make_project_tools(name)` returns callables closed over
that project's directory, so the agent for `<name>` sees only its own context
(plus cross-project `list_projects`). Each tool calls a service directly — no HTTP.

Every write tool validates before it writes and never fabricates a value: a
missing stage or column is a raised error, not an invented default."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from app.services import workspace
from app.services.loader import find_stage_file


def make_project_tools(name: str, *, examples_dir: Path) -> list[Callable[..., Any]]:
    project_dir = examples_dir / name

    def list_projects() -> list[str]:
        """List the names of every authored project in the workspace."""
        return workspace.list_project_names(examples_dir)

    def describe_workflow() -> dict[str, Any]:
        """Summarize this project's workflow: each stage's id, type, name, upstream
        input ids, and review state. Read this before editing so you know the
        current shape. Does not return full stage specs — use read_stage for one."""
        return workspace.project_workflow_summary(project_dir)

    def read_stage(stage_id: str) -> str:
        """Return the on-disk JSON of one stage. Read a stage before editing it."""
        target = find_stage_file(project_dir / "compiled", stage_id)
        if target is None:
            raise ValueError(f"no stage '{stage_id}' in project '{name}'")
        return target.read_text(encoding="utf-8")

    return [list_projects, describe_workflow, read_stage]
```

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_project_tools.py -v` → PASS (2)

- [ ] **Step 5: Typecheck + commit**
```bash
python -m mypy app/chat/project_tools.py
git add app/chat/project_tools.py tests/test_project_tools.py
git commit -m "feat(chat): project read tools (list / describe workflow / read_stage)"
git push
```

---

# Phase B — Edit tools (validated writes)

### Task B1: Extract `edit_stage_spec` service from the `node_edit` route

Extract the route's parse→validate→write→rehash core into a service so the `edit_stage` tool and the route share one writer. On the current base this is small — the loader already does file-find and JSON write.

**Files:** Create `app/services/stage_edit.py`; Modify `app/web/routers/node_review.py:131-199`; Test `tests/test_stage_edit.py`

**Interfaces produced:**
- `@dataclass EditStageResult(ok: bool, issues: list[str], content_hash: str | None, state: str | None)`.
- `edit_stage_spec(project_dir: Path, stage_id: str, spec_text: str) -> EditStageResult` — parse JSON, strip `CANONICAL_IGNORE_KEYS`, guard `id == stage_id`, `validate_stage`; on any issue return `ok=False`, write nothing; on success `write_stage` the validated Stage to its existing file. Raises `FileNotFoundError` if no such stage file exists.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_stage_edit.py
import json
from pathlib import Path

import pytest

from app.services import loader, node_review, stage_edit

_VALID = {
    "id": "score", "name": "Score rows", "type": "llm_transform",
    "inputs": [{"id": "load"}],
    "llm": {"model": "claude-sonnet-4-6", "prompt_template": "score {row}"},
}


def _seed(tmp_path: Path) -> Path:
    compiled = tmp_path / "alpha" / "compiled"
    compiled.mkdir(parents=True, exist_ok=True)
    (compiled / "02_score.json").write_text(json.dumps(_VALID), encoding="utf-8")
    return tmp_path / "alpha"


def test_valid_edit_writes_and_returns_hash_and_state(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    edited = json.dumps({**_VALID, "name": "Score every row"})
    result = stage_edit.edit_stage_spec(pdir, "score", edited)
    assert result.ok is True
    assert result.state == "unreviewed"
    assert result.content_hash
    assert "Score every row" in (pdir / "compiled" / "02_score.json").read_text(encoding="utf-8")


def test_invalid_edit_writes_nothing(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    before = (pdir / "compiled" / "02_score.json").read_text(encoding="utf-8")
    result = stage_edit.edit_stage_spec(pdir, "score", json.dumps({"id": "score", "type": "not_a_real_type", "name": "x"}))
    assert result.ok is False and result.issues
    assert (pdir / "compiled" / "02_score.json").read_text(encoding="utf-8") == before


def test_id_mismatch_rejected(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    result = stage_edit.edit_stage_spec(pdir, "score", json.dumps({**_VALID, "id": "renamed"}))
    assert result.ok is False and any("must equal" in i for i in result.issues)


def test_edit_after_approval_drops_to_edited_stale(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    # Approve the CURRENT spec (hash it the same way the service does), then edit it.
    validated = loader.Stage.model_validate(_VALID) if hasattr(loader, "Stage") else None
    from app.models import Stage
    original_hash = node_review.node_content_hash(loader.stage_to_spec_dict(Stage.model_validate(_VALID)))
    node_review.record_node_decision(pdir, stage_id="score", content_hash=original_hash,
                                     decision="approve", reviewer="human")
    result = stage_edit.edit_stage_spec(pdir, "score", json.dumps({**_VALID, "name": "Score rows v2"}))
    assert result.ok is True and result.state == "edited_stale"


def test_missing_stage_file_raises(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    with pytest.raises(FileNotFoundError):
        stage_edit.edit_stage_spec(pdir, "ghost", json.dumps({"id": "ghost", "name": "x", "type": "input_data"}))
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_stage_edit.py -v` → `ModuleNotFoundError`

- [ ] **Step 3: Implement `app/services/stage_edit.py`**

```python
"""stage_edit.py — the single validated writer for one compiled stage.

Extracted from the node-edit route so the route and the editing agent's
`edit_stage` tool share ONE writer: same validation (`validate_stage`), same
canonical form + hash (so an edit recolours the DAG identically), same refusal to
write an invalid spec. Lives here (not in node_review.py, which is free of
app.models) because validating requires the Stage model. All on-disk I/O goes
through the loader."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.models import Stage, validate_stage
from app.services import node_review
from app.services.loader import find_stage_file, stage_to_spec_dict, write_stage


@dataclass
class EditStageResult:
    ok: bool
    issues: list[str] = field(default_factory=list)
    content_hash: str | None = None
    state: str | None = None


def edit_stage_spec(project_dir: Path, stage_id: str, spec_text: str) -> EditStageResult:
    """Validate `spec_text` (a single stage as JSON) as the new spec for
    `stage_id` and, only if clean, overwrite that stage's existing compiled file.
    Returns issues (and writes nothing) on any parse/validation problem. Raises
    FileNotFoundError if no compiled file for `stage_id` exists — edit revises, it
    never creates."""
    try:
        parsed = json.loads(spec_text)
    except json.JSONDecodeError as exc:
        return EditStageResult(ok=False, issues=[f"JSON parse error: {exc}"])
    if not isinstance(parsed, dict):
        return EditStageResult(ok=False, issues=["edited spec must be a JSON object (a single stage)"])

    stage = {k: v for k, v in parsed.items() if k not in node_review.CANONICAL_IGNORE_KEYS}

    parsed_id = stage.get("id")
    if parsed_id != stage_id:
        return EditStageResult(
            ok=False,
            issues=[f"id in the edited spec ('{parsed_id}') must equal the stage id '{stage_id}'"],
        )

    issues = validate_stage(stage)
    if issues:
        return EditStageResult(ok=False, issues=issues)

    target = find_stage_file(project_dir / "compiled", stage_id)
    if target is None:
        raise FileNotFoundError(f"no existing compiled file for stage '{stage_id}' in {project_dir.name}")

    validated = Stage.model_validate(stage)
    write_stage(target, validated)

    spec = stage_to_spec_dict(validated)
    content_hash = node_review.node_content_hash(spec)
    decisions = node_review.load_node_decisions(project_dir)
    state = node_review.approval_state_for(spec, decisions)["state"]
    return EditStageResult(ok=True, content_hash=content_hash, state=state)
```

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_stage_edit.py -v` → PASS (5)

- [ ] **Step 5: Repoint the `node_edit` route.** Replace the route body after its docstring (`node_review.py` ~143-199) with a call to the service, preserving the HTTP contract:

```python
    project_dir = EXAMPLES_DIR / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    try:
        result = stage_edit.edit_stage_spec(project_dir, stage_id, spec_text)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not result.ok:
        return JSONResponse({"ok": False, "issues": result.issues}, status_code=400)
    return JSONResponse({"ok": True, "content_hash": result.content_hash, "state": result.state})
```

Add `from app.services import stage_edit`. Drop now-unused imports (`json`, `find_stage_file`, `write_stage`, `stage_to_spec_dict`, `validate_stage`, `Stage`) **only if** no other route in the file uses them (grep first — leave shared ones).

- [ ] **Step 6: Run service + route tests** — `python -m pytest tests/test_stage_edit.py tests/ -k "node_edit or node_review or edit" -v` → PASS (contract unchanged)

- [ ] **Step 7: Typecheck + commit**
```bash
python -m mypy app/services/stage_edit.py app/web/routers/node_review.py
git add app/services/stage_edit.py app/web/routers/node_review.py tests/test_stage_edit.py
git commit -m "refactor(services): extract edit_stage_spec; node_edit route delegates to it"
git push
```

---

### Task B2: `edit_stage` + `create_version` tools

**Files:** Modify `app/chat/project_tools.py`, `tests/test_project_tools.py`

**Interfaces produced (extend the factory):**
- `edit_stage(stage_id: str, spec_json: str) -> dict` — `{"ok","issues","content_hash","state"}`. Success drops the node to `edited_stale`. Never approves.
- `create_version(message: str) -> dict` — snapshot; `reviewer="agent"` (honest provenance).

- [ ] **Step 1: Add failing tests**

```python
# append to tests/test_project_tools.py
def test_edit_stage_tool_writes_and_reports_state(tmp_path: Path) -> None:
    compiled = tmp_path / "alpha" / "compiled"
    compiled.mkdir(parents=True, exist_ok=True)
    (compiled / "01_load.json").write_text(json.dumps({"id": "load", "name": "Load", "type": "input_data"}), encoding="utf-8")
    tools = project_tools.make_project_tools("alpha", examples_dir=tmp_path)
    out = _tool(tools, "edit_stage")("load", json.dumps({"id": "load", "name": "Load rows", "type": "input_data"}))
    assert out["ok"] is True and out["state"] == "unreviewed"
    assert "Load rows" in (compiled / "01_load.json").read_text(encoding="utf-8")


def test_create_version_tool_snapshots_as_agent(tmp_path: Path) -> None:
    compiled = tmp_path / "alpha" / "compiled"
    compiled.mkdir(parents=True, exist_ok=True)
    (compiled / "01_load.json").write_text(json.dumps({"id": "load", "name": "Load", "type": "input_data"}), encoding="utf-8")
    tools = project_tools.make_project_tools("alpha", examples_dir=tmp_path)
    out = _tool(tools, "create_version")("first snapshot")
    assert out["reviewer"] == "agent"
    assert (tmp_path / "alpha" / "versions" / out["id"] / "version.json").exists()
```

- [ ] **Step 2: Run to verify failure** — tool not registered

- [ ] **Step 3: Extend `make_project_tools`** (add before `return`, and expand the returned list):

```python
    from app.services import stage_edit, versioning

    def edit_stage(stage_id: str, spec_json: str) -> dict[str, Any]:
        """Replace one stage's spec with `spec_json` (the full stage as JSON). The
        spec is validated first; if invalid, nothing is written and the issues are
        returned. A successful edit drops the node to 'edited_stale' (amber) for a
        human to re-approve — you cannot approve it yourself. The `id` in the JSON
        must equal `stage_id`."""
        result = stage_edit.edit_stage_spec(project_dir, stage_id, spec_json)
        return {"ok": result.ok, "issues": result.issues,
                "content_hash": result.content_hash, "state": result.state}

    def create_version(message: str) -> dict[str, Any]:
        """Snapshot the current compiled/ (+ schemas/ if present) as an immutable
        version, freezing review coverage. Do this before regenerating from scratch
        so prior work is never lost. Recorded with reviewer='agent'."""
        existing = versioning.list_versions(project_dir)
        parent = existing[0]["id"] if existing else None
        return versioning.create_version(project_dir, message=message, reviewer="agent", parent_version=parent)

    return [list_projects, describe_workflow, read_stage, edit_stage, create_version]
```

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_project_tools.py -v` → PASS

- [ ] **Step 5: Typecheck + commit**
```bash
python -m mypy app/chat/project_tools.py
git add app/chat/project_tools.py tests/test_project_tools.py
git commit -m "feat(chat): edit_stage + create_version tools (validated write, agent snapshot, no self-approval)"
git push
```

---

# Phase C — Author + regenerate tools (doc-on-disk)

### Task C1: `fetch_document` + `read_section` + `grep_doc`

**Files:** Modify `app/chat/project_tools.py`, `tests/test_project_tools.py`

**Interfaces produced:**
- `fetch_document(src_path: str) -> dict` — copy the local file to `<project_dir>/source/<basename>`; return `{"path","bytes","lines","headings":[str]}`; never body text; raise `ValueError` if the path is absent (no URL guessing).
- `read_section(doc_path: str, heading: str) -> str` — lines from the first heading containing `heading` to the next same-or-higher heading; ≤400 lines.
- `grep_doc(doc_path: str, query: str) -> str` — ≤50 matching lines, `"<lineno>: <text>"`, case-insensitive.

Add three closures to the factory (`import shutil` at module top). Full bodies:

```python
    def fetch_document(src_path: str) -> dict[str, Any]:
        """Copy a local source document into this project's source/ folder and
        return a handle: its on-disk path plus a cheap outline (byte size, line
        count, markdown headings) — never the body. Then read bounded slices with
        read_section / grep_doc, or compile it with compile_workflow. Raises if the
        path does not exist (it is not guessed or treated as a URL)."""
        src = Path(src_path)
        if not src.is_file():
            raise ValueError(f"no document at '{src_path}' (fetch_document takes a local file path)")
        source_dir = project_dir / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        dest = source_dir / src.name
        shutil.copyfile(src, dest)
        lines = dest.read_text(encoding="utf-8", errors="replace").splitlines()
        headings = [ln for ln in lines if ln.lstrip().startswith("#")]
        return {"path": str(dest), "bytes": dest.stat().st_size, "lines": len(lines), "headings": headings}

    def read_section(doc_path: str, heading: str) -> str:
        """Return the lines under the first heading containing `heading`, up to the
        next heading of the same or higher level. Capped at 400 lines."""
        lines = Path(doc_path).read_text(encoding="utf-8", errors="replace").splitlines()
        start = next((i for i, ln in enumerate(lines)
                      if ln.lstrip().startswith("#") and heading.lower() in ln.lower()), None)
        if start is None:
            raise ValueError(f"no heading matching '{heading}' in {doc_path}")
        level = len(lines[start]) - len(lines[start].lstrip("#").lstrip())
        collected = [lines[start]]
        for ln in lines[start + 1:]:
            if ln.lstrip().startswith("#") and (len(ln) - len(ln.lstrip("#").lstrip())) <= level:
                break
            collected.append(ln)
            if len(collected) >= 400:
                break
        return "\n".join(collected)

    def grep_doc(doc_path: str, query: str) -> str:
        """Return up to 50 lines of the document matching `query` (case-insensitive),
        each prefixed with its 1-based line number."""
        needle = query.lower()
        out: list[str] = []
        for lineno, ln in enumerate(Path(doc_path).read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            if needle in ln.lower():
                out.append(f"{lineno}: {ln}")
                if len(out) >= 50:
                    break
        return "\n".join(out)
```

Tests: outline-not-body, missing-fails-loud, bounded slices (source dir under `<project>/source/`). Append the three tools to the returned list. Commit: `feat(chat): doc-on-disk tools (fetch_document outline + bounded read_section/grep_doc)`.

---

### Task C2: `compile_workflow` tool + destructive-regenerate guard

**Files:** Create `app/errors.py`; Modify `app/chat/project_tools.py`, `tests/test_project_tools.py`

**Design note (read first):** the tool writes **directly** into `examples/<name>/compiled/` via `write_methodology`, not the `compilations/` staging area (see §10-Q3 resolution). It compiles honestly: a compiler exception surfaces as an error and nothing is written; validation issues are returned and nothing is written.

**Interfaces produced:**
- `app/errors.py::RegenerateWithoutSnapshotError(Exception)`.
- `compile_workflow(doc_path: str, confirm_overwrite: bool = False) -> dict` — reads the doc, compiles, writes into `compiled/`. If any node is not `unreviewed` (review work exists), it `create_version`s first (reviewer='agent') and requires `confirm_overwrite=True`, else raises `RegenerateWithoutSnapshotError`. On compiler validation issues, nothing is written and issues are returned.

Module-top imports (so tests can monkeypatch): `from app.compiler import compile_methodology as compile_prose_to_workflow, read_input`; `from app.services.compilation import write_methodology`; `from app.services import versioning`; `from app.errors import RegenerateWithoutSnapshotError`.

```python
    def compile_workflow(doc_path: str, confirm_overwrite: bool = False) -> dict[str, Any]:
        """Compile a source document (already on disk — pass its path) into this
        project's workflow, writing every stage into compiled/ as unreviewed
        (amber). This OVERWRITES the current compiled/. If any node carries review
        work (approved/edited/rejected), pass confirm_overwrite=True; a version
        snapshot is taken first so nothing is lost. If the compiler reports
        validation issues, nothing is written and the issues are returned."""
        summary = workspace.project_workflow_summary(project_dir)
        has_review_work = any(s["review_state"] != "unreviewed" for s in summary["stages"])
        if has_review_work:
            if not confirm_overwrite:
                raise RegenerateWithoutSnapshotError(
                    f"'{name}' has reviewed stages; re-call with confirm_overwrite=True to snapshot and regenerate."
                )
            existing = versioning.list_versions(project_dir)
            parent = existing[0]["id"] if existing else None
            versioning.create_version(project_dir, message=f"pre-regenerate snapshot of {name}",
                                      reviewer="agent", parent_version=parent)
        text = read_input(doc_path)
        result = compile_prose_to_workflow(text, name)
        if result["validation"]:
            return {"ok": False, "issues": result["validation"]}
        write_methodology(result, project_dir)
        return {"ok": True, "stages": [stage["id"] for stage in result["stages"]]}
```

`app/errors.py`:
```python
"""errors.py — project exceptions, declared centrally and dependency-free so any
layer can catch them without import cycles."""

from __future__ import annotations


class RegenerateWithoutSnapshotError(Exception):
    """Raised when a from-scratch compile would overwrite reviewed work without a
    prior version snapshot and without explicit confirm_overwrite."""
```

Tests (offline — monkeypatch `read_input`/`compile_prose_to_workflow`): fresh compile writes `compiled/`; reviewed work without `confirm_overwrite` raises `RegenerateWithoutSnapshotError`, with it snapshots then overwrites; validation issues write nothing. Commit: `feat(chat): compile_workflow tool with snapshot-before-regenerate guard; fail-loud on validation issues`.

---

# Phase D — Mount the per-project editing agent

### Task D1: `build_project_agent(name)` — engine + system prompt + tools

**Files:** Create `app/chat/project_agent.py`; Test `tests/test_project_agent.py`

```python
"""project_agent.py — builds the per-project editing agent: a ChatEngine bound to
one project's tools + a system prompt naming it. One agent per project, cached.
Reuses the chat spine (turns.py + store.py + the FE) verbatim; only the tools +
prompt are project-specific."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.chat.engine import ChatEngine
from app.chat.project_tools import make_project_tools
from app.web.config import EXAMPLES_DIR

SYSTEM_PROMPT_TEMPLATE = (
    "You help a journalist author and refine the project '{name}' — a workflow of "
    "typed stages. Read before you edit (describe_workflow, read_stage). Every edit "
    "is validated and lands as UNREVIEWED (amber) for a human to approve — you "
    "cannot approve nodes. Snapshot a version (create_version) before regenerating "
    "from scratch. The source document stays on disk: fetch_document returns an "
    "outline, read_section/grep_doc return slices, compile_workflow reads the path. "
    "Never invent a column, source, model, or value — if you lack it, ask."
)

_agents: dict[str, ChatEngine] = {}


def build_project_agent(name: str, *, examples_dir: Path = EXAMPLES_DIR, model: Any = None) -> ChatEngine:
    """Construct a fresh editing agent for `name` with only that project's tools
    and a prompt naming it. model=None lets ChatEngine pick the configured backend;
    pass a model in tests to stay offline."""
    return ChatEngine(
        system_prompt=SYSTEM_PROMPT_TEMPLATE.format(name=name),
        tools=make_project_tools(name, examples_dir=examples_dir),
        model=model,
    )


def get_project_agent(name: str) -> ChatEngine:
    """Cached editing agent for `name` (built once per process)."""
    if name not in _agents:
        _agents[name] = build_project_agent(name)
    return _agents[name]
```

Test (uses `app.chat.dev_model.make_dev_model()` — offline): `build_project_agent("alpha", examples_dir=tmp_path, model=make_dev_model())` returns a `ChatEngine`; assert `make_project_tools("alpha", examples_dir=tmp_path)` yields tool names `{list_projects, describe_workflow, read_stage, edit_stage, create_version, fetch_document, read_section, grep_doc, compile_workflow}` (assert on the factory's tool names — stable — rather than PydanticAI's private registry). Commit: `feat(chat): build per-project editing agent (bound tools + system prompt, cached)`.

---

### Task D2: Mount project-scoped chat routes

**Files:** Modify `app/chat/router.py`; Test `tests/test_project_chat_routes.py`

Add (mirroring the existing `/chat/*` handlers, engine = the project agent):
```python
from app.chat.project_agent import get_project_agent


@router.post("/chat/project/{name}/sessions")
async def new_project_session(name: str):
    sid = _store.create()  # confirm create()'s real signature; set context/title via setters if needed
    return RedirectResponse(url=f"/chat/{sid}", status_code=303)


@router.post("/chat/{sid}/project/{name}/message")
async def post_project_message(sid: str, name: str, request: Request):
    if not _store.exists(sid):
        raise HTTPException(status_code=404, detail="Session not found")
    text = ((await request.json()) or {}).get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty message")
    _store.set_pending_user(sid, text)
    turn_id = _turns.start(engine=get_project_agent(name), store=_store, session_id=sid, prompt=text)
    return JSONResponse({"ok": True, "turn_id": turn_id})
```

Test with FastAPI `TestClient` + `CW_CHAT_BACKEND=dev`: POST `/chat/project/alpha/sessions` → 303; POST `/chat/{sid}/project/alpha/message` → `{ok: true}`. Confirm the app import path and `SessionStore.create` signature first. Commit: `feat(chat): project-scoped chat routes (per-project editing agent)`.

---

### Task D3: Link the project page to its editing chat

**Files:** Modify `app/templates/project.html`

```html
<form method="post" action="/chat/project/{{ project }}/sessions" style="display:inline">
  <button type="submit" title="Open a chat to edit this project">✏️ Edit with agent</button>
</form>
```
(Match the template's real context variable for the project name.) Manual integration check (needs a tool-running backend — see the ⚠️ CHECKLIST): edit a stage via chat → tool_call/tool_result bubbles render → the node goes amber in the workflow view via the existing review poll. Commit: `feat(web): link the project page to its editing-agent chat`.

---

## Self-Review

**Spec coverage vs. design doc:** AUTHOR (fetch_document+compile_workflow), EDIT (edit_stage→edited_stale), REGENERATE (snapshot-first guard), create_version-yes / approve-no, doc-on-disk, safety invariants — all present. `edit_data_model` deferred (§10-Q1). SDK-native engine + neutral store → Plan 2.

**Type consistency:** `edit_stage_spec` → `EditStageResult`; tool wraps to dict. `create_version` reviewer `"agent"`; compiler aliased `compile_prose_to_workflow` to avoid the inner-tool name shadow. All stage I/O via the loader.

**Placeholder scan:** every code step has complete code.

## ✅ CHECKLIST FOR HUMAN
- [ ] Decide compile lifecycle: agent `compile_workflow` writes **direct** into `examples/<name>/compiled/` (current plan) vs. stage into `compilations/<id>/` + a promote step (matches the UI's audited flow). Direct-write bypasses the `manifest.json`/`what_happened.json` audit trail.
- [ ] Confirm an `ANTHROPIC_API_KEY` is available for dev, or accept that live-running waits for Plan 2 (tools are unit-tested regardless).
- [ ] Confirm the FastAPI app import path (D2) and `SessionStore.create` signature before D2.

---

## Plan 2 (follow-on, not in this file): SDK-native subscription engine
After Plan 1's tools land: a `claude-agent-sdk`-native engine satisfying `stream_turn`, registering these tools as SDK-MCP in-process functions on `ClaudeAgentOptions` (CLI runs the tool loop), reusing `app/runtime/llm_agent_sdk.py`'s block→event mapping; make `store.py` engine-neutral. Same tools, subscription, no API key.

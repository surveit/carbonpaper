# llm_transform via structured-output Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the raw-text LLM backends of `llm_transform` with the headless structured-output `Agent` (app/agent/agent.py), so every reply is a validated Pydantic instance of the stage's reply spec — never parsed text.

**Architecture:** A new core primitive `build_row_model` compiles a `TableSchema` into a Pydantic model (recursively — `list[json]` element fields included). The `llm_transform` mapper builds that model from the reply spec (`output_schema.subtract(input_schema)`) and hands it to `Agent` as `target_schema`; the agent submits its answer through the `submit_answer` tool whose input schema IS the model, retrying in-loop on validation failure. The old `llm_agent_sdk` raw-text backend, the `claude -p` subprocess backend, and all text-parse heuristics (fence stripping, last-JSON extraction) are deleted. Backends become `agent | mock`; the mock stays opt-in-only (never a silent fallback). Import direction becomes `app.runtime → app.agent → app.core` (today `app.agent → app.runtime` exists only for `_CLI_PATH`); both import-linter contracts are updated as deliberate edits.

**Tech Stack:** Python 3.12, Pydantic v2 (`create_model`), claude-agent-sdk (already a hard dep in requirements.txt), pytest, mypy, ruff, import-linter.

## Global Constraints

- **Never fabricate; fail loudly.** No silent fallback to the mock; a live-backend failure raises. A non-dict mock reply raises. (Repo cardinal rule.)
- **No `Any` to silence mypy; no `# type: ignore`.** If mypy flags `Literal[tuple(...)]` as an expression, use `Literal.__getitem__(tuple(column.enum))` or restructure — do not ignore.
- **Runtime-neutral epistemics:** the runtime's system prompt frames only the calling convention. No "use null when unsure"-style guidance — that is compiler-authored prompt content.
- **Exceptions live in `app/core/errors.py`**, not inline in feature modules. `LLMError` moves there.
- **Function names start with a verb** (`build_row_model`, not `row_model`).
- **Comments describe the interface as it is now** — never narrate the change ("no longer parses text" is banned wording).
- Work in worktree `C:\journalism_sprint\prototype_one_llm_agent_wt`, branch `llm-transform-agent`. Push every commit: `git push -u origin llm-transform-agent` (first push), then `git push`.
- Verify commands: `ruff check app tests`, `mypy app`, `lint-imports`, `pytest -q` (offline: conftest forces the mock).

---

### Task 1: `build_row_model` — compile a TableSchema to a Pydantic model

**Files:**
- Create: `app/core/models/row_model.py`
- Modify: `app/core/models/__init__.py` (export `build_row_model`)
- Test: `tests/test_row_model.py`

**Interfaces:**
- Consumes: `TableSchema`, `Column`, `_LIST_RE` from `app/core/models/schema.py`; `SCALAR_COLUMN_TYPES` semantics (str/int/float/bool/date/datetime).
- Produces: `build_row_model(schema: TableSchema, name: str) -> type[BaseModel]` — used by Task 2's mapper. Field semantics later tasks rely on: every column is a **required** field (nullable ⇒ the value may be `None`, but the key must be present); `extra="forbid"`; enum → `Literal`; numeric `range` → `ge`/`le` (string bounds containing `"inf"` mean unbounded); `json`+`fields` → nested model; `list[json]`+`fields` → list of nested models; `value_type` → `dict[str, <scalar>]`; `description` → `Field(description=...)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_row_model.py
"""build_row_model: a TableSchema compiled to a Pydantic model enforces the
schema recursively — presence, types, nullability, enum vocabulary, numeric
range, nested json/list[json] fields — and rejects unknown keys."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.models import TableSchema
from app.core.models.row_model import build_row_model


def _model(cols):
    return build_row_model(TableSchema.model_validate({"columns": cols}), "reply")


def test_valid_row_roundtrips():
    model = _model([
        {"name": "score", "type": "int", "nullable": False},
        {"name": "note", "type": "str"},
    ])
    got = model.model_validate({"score": 3, "note": None})
    assert got.model_dump() == {"score": 3, "note": None}


def test_missing_key_rejected():
    # nullable ≠ omittable: every declared column must appear in the reply
    model = _model([{"name": "note", "type": "str"}])
    with pytest.raises(ValidationError):
        model.model_validate({})


def test_null_in_non_nullable_rejected():
    model = _model([{"name": "score", "type": "int", "nullable": False}])
    with pytest.raises(ValidationError):
        model.model_validate({"score": None})


def test_unknown_key_rejected():
    model = _model([{"name": "score", "type": "int", "nullable": False}])
    with pytest.raises(ValidationError):
        model.model_validate({"score": 1, "bonus": 2})


def test_enum_vocabulary_enforced():
    model = _model([{"name": "stance", "type": "str", "nullable": False,
                     "enum": ["supports", "opposes"]}])
    assert model.model_validate({"stance": "supports"}).model_dump() == {"stance": "supports"}
    with pytest.raises(ValidationError):
        model.model_validate({"stance": "meh"})


def test_numeric_range_enforced():
    model = _model([{"name": "score", "type": "int", "nullable": False, "range": [0, 5]}])
    assert model.model_validate({"score": 5}).model_dump() == {"score": 5}
    with pytest.raises(ValidationError):
        model.model_validate({"score": 6})


def test_inf_range_bound_means_unbounded():
    model = _model([{"name": "usd", "type": "float", "nullable": False, "range": [0, "+inf"]}])
    assert model.model_validate({"usd": 1e12}).model_dump() == {"usd": 1e12}
    with pytest.raises(ValidationError):
        model.model_validate({"usd": -1.0})


def test_list_of_scalars():
    model = _model([{"name": "tags", "type": "list[str]", "nullable": False}])
    assert model.model_validate({"tags": ["a", "b"]}).model_dump() == {"tags": ["a", "b"]}
    with pytest.raises(ValidationError):
        model.model_validate({"tags": "not-a-list"})


def test_list_json_elements_validated_recursively():
    model = _model([
        {"name": "claims", "type": "list[json]", "nullable": False, "fields": [
            {"name": "text", "type": "str", "nullable": False},
            {"name": "stance", "type": "str", "nullable": False,
             "enum": ["supports", "opposes"]},
        ]},
    ])
    ok = model.model_validate(
        {"claims": [{"text": "t", "stance": "supports"}]})
    assert ok.model_dump() == {"claims": [{"text": "t", "stance": "supports"}]}
    with pytest.raises(ValidationError):  # bad element enum, one level down
        model.model_validate({"claims": [{"text": "t", "stance": "meh"}]})
    with pytest.raises(ValidationError):  # missing element key, one level down
        model.model_validate({"claims": [{"text": "t"}]})


def test_json_value_type_open_map():
    model = _model([{"name": "meta", "type": "json", "nullable": False,
                     "value_type": "int"}])
    assert model.model_validate({"meta": {"a": 1}}).model_dump() == {"meta": {"a": 1}}
    with pytest.raises(ValidationError):
        model.model_validate({"meta": {"a": "x"}})


def test_description_carried_into_json_schema():
    model = _model([{"name": "score", "type": "int", "nullable": False,
                     "description": "0 worst, 5 best"}])
    props = model.model_json_schema()["properties"]
    assert props["score"]["description"] == "0 worst, 5 best"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_row_model.py -q`
Expected: FAIL — `ModuleNotFoundError: app.core.models.row_model`

- [ ] **Step 3: Implement `app/core/models/row_model.py`**

```python
"""Compile a TableSchema into a Pydantic model: one field per column.

`build_row_model(schema, name)` returns a Pydantic model class whose fields
mirror the schema's columns — name, scalar type, nullability, enum vocabulary,
numeric range, and description all carry over, and a `json`/`list[json]`
column with `fields` becomes a nested model, validated recursively. Every
column is a REQUIRED field: `nullable` permits a None value, not an absent
key. Unknown keys are rejected.

Named consumer: app.runtime.stages.llm_transform compiles a stage's reply spec
with this and hands the model to app.agent.agent.Agent as `target_schema`, so
the reply spec is enforced (the agent must submit a validating instance)
rather than merely described in prompt prose.
"""
from __future__ import annotations

import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, create_model

from app.core.models.schema import Column, TableSchema, _LIST_RE

_SCALAR_PY_TYPES: dict[str, type] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "date": datetime.date,
    "datetime": datetime.datetime,
}


def build_row_model(schema: TableSchema, name: str) -> type[BaseModel]:
    return _build_model(name, list(schema.columns))


def _build_model(name: str, columns: list[Column]) -> type[BaseModel]:
    field_definitions: dict[str, Any] = {}
    for column in columns:
        annotation = _annotation_for(column, parent_name=name)
        if column.nullable:
            annotation = Optional[annotation]
        field_definitions[column.name] = (annotation, _field_for(column))
    return create_model(
        name, __config__=ConfigDict(extra="forbid"), **field_definitions
    )


def _annotation_for(column: Column, parent_name: str) -> Any:
    if column.type in ("json", "list[json]"):
        inner: Any
        if column.fields is not None:
            inner = _build_model(f"{parent_name}__{column.name}", list(column.fields))
        else:
            assert column.value_type is not None  # Column._json_shape enforces
            inner = dict[str, _SCALAR_PY_TYPES[column.value_type]]
        return list[inner] if column.type == "list[json]" else inner
    if column.enum is not None:
        return Literal[tuple(column.enum)]
    return _scalar_or_list_annotation(column.type)


def _scalar_or_list_annotation(type_name: str) -> Any:
    if type_name in _SCALAR_PY_TYPES:
        return _SCALAR_PY_TYPES[type_name]
    match = _LIST_RE.match(type_name)
    if match:
        return list[_scalar_or_list_annotation(match.group(1).strip())]
    raise ValueError(f"unknown column type {type_name!r}")


def _field_for(column: Column) -> Any:
    kwargs: dict[str, Any] = {}
    if column.description:
        kwargs["description"] = column.description
    low, high = _numeric_bounds(column)
    if low is not None:
        kwargs["ge"] = low
    if high is not None:
        kwargs["le"] = high
    return Field(**kwargs)


def _numeric_bounds(column: Column) -> tuple[Any, Any]:
    """A declared numeric range as (ge, le); a string bound containing "inf"
    (the schema's unbounded sentinel) becomes None on that side."""
    if column.range is None or column.type not in ("int", "float"):
        return (None, None)
    low, high = column.range
    if isinstance(low, str):
        low = None
    if isinstance(high, str):
        high = None
    return (low, high)
```

mypy notes (do NOT ignore-comment these away): if `Literal[tuple(column.enum)]` is rejected as a type expression, use `Literal.__getitem__(tuple(column.enum))`; if `list[inner]` / `dict[str, ...]` runtime subscripts are flagged, route them through a local variable annotated `Any`. The public function stays precisely typed: `(TableSchema, str) -> type[BaseModel]`.

- [ ] **Step 4: Export from the package**

In `app/core/models/__init__.py`, add `build_row_model` to the imports/`__all__` following the file's existing pattern.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_row_model.py -q`
Expected: all PASS

- [ ] **Step 6: Quality gates + commit**

Run: `ruff check app tests && mypy app && python -m pytest -q`
Expected: clean. Then:

```bash
git add app/core/models/row_model.py app/core/models/__init__.py tests/test_row_model.py
git commit -m "feat(core): build_row_model compiles a TableSchema to a Pydantic model"
git push -u origin llm-transform-agent
```

---

### Task 2: Rewire `llm_transform` onto the Agent; collapse backends to `agent | mock`

**Files:**
- Modify: `app/core/errors.py` (add `LLMError`)
- Modify: `app/runtime/options.py` (backend selection: `agent | mock`; stop importing `llm_agent_sdk`)
- Modify: `app/runtime/llm.py` (dispatch to `Agent`; delete subprocess + text-parse machinery)
- Modify: `app/runtime/stages/llm_transform.py` (build reply model; stop appending `to_prompt`)
- Modify: `pyproject.toml` (import-linter: allow `app.runtime` to import `app.agent`)
- Modify: `tests/test_llm_backend.py` (rewrite decision matrix)
- Modify: `tests/test_llm_transform_spec.py` (reply model replaces prompt-append assertions)
- Delete: `tests/test_llm_json.py` (tests text-parse helpers that no longer exist)
- Check: `tests/test_column_projection.py`, `tests/test_mock_honesty.py` (adjust only if they touch `call_llm`'s signature)

**Interfaces:**
- Consumes: `build_row_model(schema, name)` from Task 1; `Agent(system_prompt=..., target_schema=..., task=..., model=...)` with `async run() -> Model` from `app/agent/agent.py`; `run_sync(coro)` and `CLI_PATH` from `app/core/llm_sdk.py`.
- Produces: `call_llm(stage_id: str, llm_config: LLMConfig, input_row: dict[str, Any], *, reply_model: type[BaseModel], use_real: bool | None = None, model: str | None = None) -> dict[str, Any]`; `get_llm_call_type() -> str` returning `"agent" | "mock"`; `agent_available() -> bool`; `LLMError` importable from `app.core.errors` (and re-exported by `app.runtime.options` for existing callers).

- [ ] **Step 1: Move `LLMError` to `app/core/errors.py`**

Append to `app/core/errors.py` (match the file's docstring style):

```python
class LLMError(Exception):
    """A live-LLM call failed, or no LLM backend is available."""
```

In `app/runtime/options.py`, replace the inline class with `from app.core.errors import LLMError` (keep the name importable from `options` so existing `options.LLMError` callers stay valid). Grep for other importers: `grep -rn "LLMError" --include="*.py" app tests` and point them at either import path consistently (prefer `app.core.errors`).

- [ ] **Step 2: Rewrite the backend-selection tests (failing first)**

Replace `tests/test_llm_backend.py` wholesale:

```python
"""get_llm_call_type() decision matrix — fully hermetic via monkeypatch.

The key policy: a live backend that isn't available RAISES — we never silently
fall back to the mock. `mock` is reachable only when explicitly requested.
"""
from __future__ import annotations

import pytest

from app.core.errors import LLMError
from app.runtime import options


def _set(monkeypatch, *, force_mock=False, backend=None, agent=False):
    if force_mock:
        monkeypatch.setenv("CW_LLM_FORCE_MOCK", "1")
    else:
        monkeypatch.delenv("CW_LLM_FORCE_MOCK", raising=False)
    if backend is None:
        monkeypatch.delenv("CW_LLM_BACKEND", raising=False)
    else:
        monkeypatch.setenv("CW_LLM_BACKEND", backend)
    monkeypatch.setattr(options, "agent_available", lambda: agent)


def test_force_mock_overrides_everything(monkeypatch):
    _set(monkeypatch, force_mock=True, backend="agent", agent=True)
    assert options.get_llm_call_type() == "mock"


def test_explicit_mock(monkeypatch):
    _set(monkeypatch, backend="mock", agent=True)
    assert options.get_llm_call_type() == "mock"


def test_auto_picks_agent_when_available(monkeypatch):
    _set(monkeypatch, agent=True)
    assert options.get_llm_call_type() == "agent"


def test_auto_without_agent_raises_never_mocks(monkeypatch):
    _set(monkeypatch, agent=False)
    with pytest.raises(LLMError):
        options.get_llm_call_type()


def test_explicit_agent_unavailable_raises(monkeypatch):
    _set(monkeypatch, backend="agent", agent=False)
    with pytest.raises(LLMError):
        options.get_llm_call_type()


def test_unknown_backend_value_raises(monkeypatch):
    _set(monkeypatch, backend="cli", agent=True)
    with pytest.raises(LLMError):
        options.get_llm_call_type()
```

Run: `python -m pytest tests/test_llm_backend.py -q` — Expected: FAIL (`agent_available` doesn't exist yet).

- [ ] **Step 3: Rewrite `app/runtime/options.py`**

```python
"""
Runtime LLM configuration + backend selection.

Isolated here (rather than inline in `llm.py`) so the env knobs and the
"which backend runs" policy live in one place an org can override without
touching the call machinery.
"""

from __future__ import annotations

import importlib.util
import os
import shutil

from app.core.errors import LLMError
from app.core.llm_sdk import CLI_PATH

__all__ = [
    "CLAUDE_BIN", "DEFAULT_MODEL", "DEFAULT_PARALLEL", "DEFAULT_TIMEOUT_S",
    "LLMError", "agent_available", "get_llm_call_type",
]

# ── Config knobs (env-overridable) ───────────────────────────────────────────
CLAUDE_BIN = shutil.which("claude") or CLI_PATH
DEFAULT_MODEL = os.environ.get("CW_LLM_MODEL", "haiku")
DEFAULT_PARALLEL = int(os.environ.get("CW_LLM_PARALLEL", "4"))
DEFAULT_TIMEOUT_S = int(os.environ.get("CW_LLM_TIMEOUT_S", "180"))


def agent_available() -> bool:
    """True when the structured-output agent backend can run: the
    claude-agent-sdk package is importable AND a Claude CLI was located."""
    return CLAUDE_BIN is not None and importlib.util.find_spec("claude_agent_sdk") is not None


def get_llm_call_type() -> str:
    """Pick the LLM backend: ``'agent'`` | ``'mock'``.

    - ``CW_LLM_FORCE_MOCK=1`` → ``'mock'``.
    - ``CW_LLM_BACKEND`` selects explicitly: ``agent`` | ``mock``.
    - default ``auto`` → ``agent`` when available.

    We never silently fall back to the mock. If a live backend is requested (or
    ``auto``) but none is available, we raise — a mock result must never be
    mistaken for a real model answer. ``mock`` is reachable only when the caller
    explicitly asks for it (``CW_LLM_FORCE_MOCK=1`` or ``CW_LLM_BACKEND=mock``).
    """
    if os.environ.get("CW_LLM_FORCE_MOCK") == "1":
        return "mock"
    choice = os.environ.get("CW_LLM_BACKEND", "auto").lower()
    if choice == "mock":
        return "mock"
    if choice in ("auto", "agent"):
        if agent_available():
            return "agent"
        raise LLMError(
            "No live LLM backend available (claude-agent-sdk isn't importable "
            "or the claude CLI wasn't found). Install them, or set "
            "CW_LLM_FORCE_MOCK=1 to run the offline mock."
        )
    raise LLMError(f"CW_LLM_BACKEND={choice!r}: expected one of agent, mock, auto")
```

Run: `python -m pytest tests/test_llm_backend.py -q` — Expected: PASS.

- [ ] **Step 4: Rewrite `app/runtime/llm.py`**

Delete `_call_claude_subprocess`, `_parse_text_result`, `_extract_last_json`, `_parse_inner_result`, `call_llm_real`, and the `llm_agent_sdk` import. Keep `render_prompt` verbatim. New module:

```python
"""LLM dispatch for `llm_transform` stages.

`call_llm` routes one input row to the active backend chosen by
`options.get_llm_call_type()`: a headless structured-output agent
(`app.agent.agent.Agent`) whose `target_schema` is the stage's reply model —
the reply arrives as a validated Pydantic instance submitted through the
agent's submit_answer tool — or the opt-in offline mock (`llm_mock`). Backends
never silently fall back to the mock: a missing or failed live backend raises
rather than fabricating output.

Batching: the runtime's row driver (`app/runtime/stages/execution.py`) calls
`call_llm` once per row under bounded parallelism (default 4, override via
CW_LLM_PARALLEL).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from pydantic import BaseModel

from app.agent.agent import Agent
from app.core.errors import LLMError
from app.core.llm_sdk import run_sync
from app.core.models import LLMConfig

from . import llm_mock
from .options import (
    CLAUDE_BIN,
    DEFAULT_MODEL,
    DEFAULT_PARALLEL,
    DEFAULT_TIMEOUT_S,
    get_llm_call_type,
)

# Frames the calling convention only. Epistemic guidance (when a value is
# unknowable, how to weigh sources) is compiler-authored prompt content, not
# the runtime's voice.
SYSTEM_PROMPT = (
    "You are executing one transform step of a data pipeline. Work from the "
    "task input you are given. Produce the required output by calling the "
    "submit_answer tool exactly once; its input schema is the required reply."
)


def render_prompt(template: str, row: dict[str, Any]) -> str:
    # ... keep the existing implementation verbatim (including _Defaults) ...


def call_llm(
    stage_id: str,
    llm_config: LLMConfig,
    input_row: dict[str, Any],
    *,
    reply_model: type[BaseModel],
    use_real: bool | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Single-row LLM call; returns the reply as a plain dict.

    Live path: a structured-output Agent must submit a valid `reply_model`
    instance (validated by construction, retried in-loop on rejection).
    `use_real=False` (or CW_LLM_FORCE_MOCK=1) selects the offline mock — the
    only way to reach it. A live backend that errors raises rather than
    degrading to the mock, so a fabricated answer never masquerades as a real
    model reply."""
    backend = "mock" if use_real is False else get_llm_call_type()

    if backend == "mock":
        reply = llm_mock.mock_llm_call(stage_id, llm_config, input_row)
        if not isinstance(reply, dict):
            raise LLMError(
                f"stage {stage_id}: mock returned {type(reply).__name__}, expected a dict"
            )
        return reply

    if not llm_config.prompt_template:
        raise LLMError(f"stage {stage_id}: llm_transform has no prompt_template")
    if llm_config.tools:
        raise LLMError(
            f"stage {stage_id}: llm.tools is not supported by the agent backend"
        )
    prompt = render_prompt(llm_config.prompt_template, input_row)
    agent: Agent[BaseModel] = Agent(
        system_prompt=SYSTEM_PROMPT,
        target_schema=reply_model,
        task=prompt,
        model=str(model or llm_config.model or DEFAULT_MODEL),
    )
    answer = run_sync(asyncio.wait_for(agent.run(), timeout=DEFAULT_TIMEOUT_S))
    return answer.model_dump(mode="json")


def backend_status() -> dict[str, Any]:
    """For UI/diagnostics: report which backend is active (or why none is)."""
    try:
        backend: str | None = get_llm_call_type()
        backend_error = None
    except LLMError as exc:
        backend = None
        backend_error = str(exc)
    return {
        "backend": backend,
        "backend_error": backend_error,
        "claude_cli": CLAUDE_BIN,
        "model_default": DEFAULT_MODEL,
        "parallel_default": DEFAULT_PARALLEL,
        "force_mock": os.environ.get("CW_LLM_FORCE_MOCK") == "1",
    }
```

Notes: keep the existing `render_prompt` body exactly (it needs `json`). If `mock_llm_call`'s return type already guarantees dict per its signature, keep the isinstance guard anyway — the mock is pattern-matching code and this is the boundary where a wrong shape must fail loudly. Check `LLMConfig.model`'s runtime type (`use_enum_values` may make it str) — `str(...)` covers both.

- [ ] **Step 5: Update the spec tests for the mapper (failing first)**

In `tests/test_llm_transform_spec.py`: update the module docstring ("the one thing llm_transform adds over a plain LLM call: it compiles the derived reply spec — output_schema − input_schema — to the Pydantic model the agent backend enforces"). Replace `test_reply_spec_appended_to_prompt` and `test_list_reply_is_a_value_not_rows` with:

```python
def test_reply_model_is_the_subtracted_spec(monkeypatch):
    captured: dict[str, object] = {}

    def fake_call(stage_id, llm_config, row, *, reply_model, **kw):
        captured["fields"] = set(reply_model.model_fields)
        captured["template"] = llm_config.prompt_template
        return {"score": 5}

    monkeypatch.setattr(lt, "call_llm", fake_call)
    _run(_stage(), {"load": pd.DataFrame({"id": ["r1"], "text": ["hi"]})})

    assert captured["fields"] == {"score"}            # added column asked for…
    # …passthrough columns are not: they ride through from the input row.
    assert captured["template"] == "Rate: {text}"     # template reaches the backend unaltered


def test_reply_model_enforces_the_spec():
    # the model built for the stage rejects a wrong-shaped reply outright
    from app.core.models.row_model import build_row_model
    stage = _stage()
    spec = stage.output_schema.subtract(stage.inputs[0].table_schema)
    model = build_row_model(spec, "score_reply")
    with pytest.raises(ValidationError):
        model.model_validate({"score": "not-a-number-at-all"})
```

(add `import pytest` / `from pydantic import ValidationError` as needed). Keep `test_output_rows_carry_reply_columns` and `test_backend_error_is_recorded_per_row_not_raised` — they must pass unchanged, except any `fake_call` signatures gain `**kw` tolerance if they don't have it.

Run: `python -m pytest tests/test_llm_transform_spec.py -q` — Expected: FAIL (mapper still appends prompt spec; `call_llm` lacks `reply_model`).

- [ ] **Step 6: Rewrite `app/runtime/stages/llm_transform.py`**

```python
"""Row mapper for the llm_transform stage type.

Runs the stage's prompt over each input row via the LLM layer (`llm.call_llm`;
the runtime's row driver supplies bounded parallelism and reassembles results
in input order). The columns `output_schema` adds beyond the input schema are
the reply spec, compiled by `build_row_model` into the Pydantic model the
agent backend enforces — a live reply is a validated instance of it, so reply
columns arrive typed. The strictly-1:1 shape holds by construction: the mapper
returns exactly one dict per input row; `Stage` validation fixes the schema
shape at construction time."""

from __future__ import annotations

from typing import Any, Callable

from app.core.models import Stage
from app.core.models.row_model import build_row_model

from ..llm import backend_status, call_llm
from .execution import Row


def make_llm_row_mapper(stage: Stage, ctx: dict[str, Any]) -> Callable[[Row], Row]:
    llm = stage.llm
    assert llm is not None  # Stage validation: llm_transform carries llm

    # The reply spec (output_schema − input_schema), compiled to the model the
    # agent must satisfy. Stage validation guarantees an llm_transform is 1:1
    # (both schemas present, output ⊇ input), so subtract never throws here.
    input_schema = stage.inputs[0].table_schema
    assert stage.output_schema is not None and input_schema is not None
    reply_spec = stage.output_schema.subtract(input_schema)
    reply_model = build_row_model(reply_spec, f"{stage.id}_reply")

    # Record which backend handled this stage so the UI/manifest can label it.
    ctx.setdefault("llm_backend", {})[stage.id] = backend_status()

    def map_row(row: Row) -> Row:
        try:
            reply = call_llm(stage.id, llm, row, reply_model=reply_model)
        except Exception as exc:  # noqa: BLE001 — per-row supervisor: any backend
            # failure (agent error, timeout, validation exhaustion, …) is
            # recorded as _error so one bad row can't abort the stage;
            # surfaced, not swallowed.
            return {**row, "_error": str(exc)}
        return {**row, **reply}

    return map_row
```

- [ ] **Step 7: Import-linter — permit the new edge**

In `pyproject.toml`, contract "app.agent spine imported only by its registrar and the entrypoint": add `"app.runtime"` to `allowed_importers` and extend the comment: `app.runtime drives it as the llm_transform reply backend.`

- [ ] **Step 8: Delete `tests/test_llm_json.py`; sweep remaining tests**

`git rm tests/test_llm_json.py` (it exercises the deleted text-parse helpers). Then `grep -rn "call_llm\|llm_agent_sdk\|call_llm_real\|_parse_text_result" tests/` — update any fake `call_llm` to accept `*, reply_model, **kw`. `tests/test_mock_honesty.py` touches only `llm_mock` directly and should pass unchanged.

- [ ] **Step 9: Full verify + commit**

Run: `ruff check app tests && mypy app && lint-imports && python -m pytest -q`
Expected: all clean. (`app/runtime/llm_agent_sdk.py` still exists and is still imported by `app/agent/sdk_engine.py` — that's Task 3.)

```bash
git add -A
git commit -m "feat(runtime): llm_transform replies via the structured-output Agent

Backends collapse to agent|mock; the reply spec is enforced as the agent's
target_schema instead of rendered into the prompt. LLMError moves to
app.core.errors. app.runtime is now an allowed importer of app.agent."
git push
```

---

### Task 3: Delete the raw-text SDK backend; retire the agent→runtime edge; docs

**Files:**
- Delete: `app/runtime/llm_agent_sdk.py`
- Modify: `app/agent/sdk_engine.py:34-37` (import `CLI_PATH` from `app.core.llm_sdk`)
- Modify: `pyproject.toml` (remove `app.agent` from `app.runtime`'s allowed importers — only if no agent→runtime import remains)
- Modify: `app/runtime/AGENTS.md` (backend description)
- Check: `app/templates/`, `docs/`, root `AGENTS.md`/`architecture.md` for stale `agent_sdk` / `CW_LLM_BACKEND=cli` references

**Interfaces:**
- Consumes: `CLI_PATH` from `app/core/llm_sdk.py` (same value `llm_agent_sdk._CLI_PATH` aliased).
- Produces: nothing new — this task only removes.

- [ ] **Step 1: Repoint `sdk_engine`'s CLI path**

In `app/agent/sdk_engine.py` replace:

```python
# `_CLI_PATH` is the located Claude Code CLI (the SDK does not always find it on
# PATH on Windows); reuse llm_agent_sdk's resolution so this engine and the
# runtime backend agree on which CLI to spawn.
from app.runtime.llm_agent_sdk import _CLI_PATH
```

with:

```python
# `CLI_PATH` is the located Claude Code CLI (the SDK does not always find it on
# PATH on Windows — app.core.llm_sdk probes the known install locations).
from app.core.llm_sdk import CLI_PATH as _CLI_PATH
```

- [ ] **Step 2: Delete the module and sweep references**

```bash
git rm app/runtime/llm_agent_sdk.py
grep -rn "llm_agent_sdk" --include="*.py" app tests
```

Expected: zero hits. Any hit is a missed caller — fix it, don't stub it.

- [ ] **Step 3: Tighten the runtime contract**

Confirm no agent→runtime import remains: `grep -rn "app.runtime" --include="*.py" app/agent/` → expected zero hits. Then in `pyproject.toml`, contract "app.runtime imported only by agent, evals, web": remove `"app.agent"` from `allowed_importers`, rename the contract to "app.runtime imported only by evals, web", and update its comment (the runner is driven from web and evals; the agent spine sits below the runtime now). If the grep DOES hit something, stop and surface it rather than leaving the contract loose silently.

- [ ] **Step 4: Docs sweep**

- `app/runtime/AGENTS.md`: describe the two backends as they now are (structured-output agent | opt-in mock), the `CW_LLM_BACKEND=agent|mock` knob, and that reply validation is the agent's target_schema. Only what is true.
- `grep -rn "agent_sdk\|CW_LLM_BACKEND" --include="*.html" --include="*.md" app docs AGENTS.md` — fix stale mentions (e.g. a template reading `backend_status()["agent_sdk"]`, docs offering `CW_LLM_BACKEND=cli`).

- [ ] **Step 5: Full verify + commit**

Run: `ruff check app tests && mypy app && lint-imports && python -m pytest -q`
Expected: all clean.

```bash
git add -A
git commit -m "refactor(runtime): delete the raw-text agent-SDK backend; agent spine no longer imports runtime"
git push
```

---

### Task 4: PR

- [ ] **Step 1: Open the PR against master**

Use the full gh path (stale-PATH machine quirk):

```bash
& "C:\Program Files\GitHub CLI\gh.exe" pr create --repo surveit/data_workflow \
  --base master --head llm-transform-agent \
  --title "llm_transform replies via structured-output Agent; drop raw-text backends"
```

Body must include: what's enforced now (reply = validated Pydantic instance, recursive over `list[json]` fields, in-loop retry via submit_answer tool errors); behavior changes (reply spec no longer appended to prompt — carried by the tool schema; `CW_LLM_BACKEND=cli` removed; `llm.tools` now raises instead of enabling web research; per-row timeout retained via `CW_LLM_TIMEOUT_S`); the import-direction flip (runtime → agent, both contracts edited deliberately); follow-ups (tools/web-research support on the Agent, mock replies don't validate against reply models by design).

- [ ] **Step 2: Report the PR URL** with the 🟣 prefix.

# SYW positional tracer (v1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Given a claim's row in a completed run, walk its ancestry backward through row-preserving stages by row ordinal alone, stopping honestly at the first stage that reshapes rows — no recording, no runtime change.

**Architecture:** A self-contained pure function `trace_row(run_dir, stage_id, row_ordinal) -> Trace` in `app/runtime/trace.py`. It reads only the run directory: `manifest.json` gives each stage's type, parent edges (the `input:<parent>` validation phases), and row counts; `outputs/<stage>.parquet` gives row values by position. It never reads the compiled DAG, so it is robust to the methodology being edited after the run. A thin JSON endpoint exposes it. The reader-facing HTML hop-card view is a deliberate follow-up (separate plan) — this plan delivers the tested engine plus its API.

**Tech Stack:** Python 3.12, pandas, pytest. FastAPI (existing app) for the endpoint only.

## Global Constraints

- Offline and deterministic: every test builds its own run directory in `tmp_path`; no LLM, no network, no dependency on `examples/` (which is gitignored on `master`).
- Row-preserving stage types (v1): `input_data`, `python_row_function`. Nothing else. `llm_transform` becomes row-preserving only once [PR #29](https://github.com/surveit/data_workflow/pull/29) lands — tracked by [issue #61](https://github.com/surveit/data_workflow/issues/61). Row-reshaping stages (`join`, `aggregate`, `python_frame_function`, fan-out) need recorded edges — [issue #58](https://github.com/surveit/data_workflow/issues/58).
- Cardinal rule: never assume position aligns. A hop is crossed only when the child stage type is row-preserving AND child/parent row counts are equal; otherwise stop with an explicit reason. Out-of-range row ordinals raise, never clamp.
- The tracer must not import from `app/web` (arrows point toward core, not away). It reads files with `json` + `pandas` directly.
- Match the existing test convention: files under `tests/`, plain `pytest`, no network.

---

### Task 1: Tracer module skeleton — types, constants, manifest/edge readers

**Files:**
- Create: `app/runtime/trace.py`
- Test: `tests/test_trace_helpers.py`

**Interfaces:**
- Produces: `ROW_PRESERVING: frozenset[str]`; dataclasses `StopReason(kind: str, stage_id: str, message: str)`, `Hop(stage_id, stage_type, row_ordinal, row, columns_new, origin)`, `Trace(run_id, start_stage, start_row, hops, terminal)`; helpers `_load_manifest(run_dir: Path) -> dict`, `_stages_by_id(manifest: dict) -> dict[str, dict]`, `_parents(stage_record: dict) -> list[str]`, `_origin(stage_type: str) -> str`; a test-only fixture builder `write_run` (defined in the test file, reused by later tasks).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trace_helpers.py
"""Unit tests for the low-level readers in app/runtime/trace.py, plus the
shared `write_run` fixture builder the later trace tests reuse."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from app.runtime.trace import (
    ROW_PRESERVING,
    _load_manifest,
    _origin,
    _parents,
    _stages_by_id,
)


def write_run(tmp_path: Path, stages: list[dict], run_id: str = "T1") -> Path:
    """Build a minimal run directory from a list of stage specs and return it.

    Each spec: {"id": str, "type": str, "parents": list[str], "df": DataFrame}.
    Writes outputs/<id>.parquet and a manifest.json whose per-stage records
    carry `type`, `rows`, `output_path`, and one input_validation entry per
    parent with phase "input:<parent>" — the exact shape the runner emits.
    """
    run_dir = tmp_path / run_id
    (run_dir / "outputs").mkdir(parents=True)
    records = []
    for spec in stages:
        rel = f"outputs/{spec['id']}.parquet"
        spec["df"].to_parquet(run_dir / rel, index=False)
        records.append({
            "stage_id": spec["id"],
            "type": spec["type"],
            "rows": len(spec["df"]),
            "output_path": rel,
            "input_validation": [
                {"phase": f"input:{p}", "ok": True} for p in spec.get("parents", [])
            ],
        })
    (run_dir / "manifest.json").write_text(
        json.dumps({"run_id": run_id, "stages": records}), encoding="utf-8"
    )
    return run_dir


def test_row_preserving_set_is_exactly_the_two_v1_types():
    assert ROW_PRESERVING == frozenset({"input_data", "python_row_function"})


def test_parents_reads_input_phases_and_ignores_output_phase():
    record = {
        "input_validation": [
            {"phase": "input:seeds"},
            {"phase": "input:other"},
        ],
    }
    assert _parents(record) == ["seeds", "other"]
    assert _parents({"input_validation": []}) == []
    assert _parents({}) == []


def test_origin_maps_stage_type_to_label():
    assert _origin("input_data") == "source"
    assert _origin("python_row_function") == "computed"
    assert _origin("llm_transform") == "llm"
    assert _origin("join") == "other"


def test_load_manifest_and_stages_by_id(tmp_path):
    run_dir = write_run(tmp_path, [
        {"id": "seeds", "type": "input_data", "parents": [],
         "df": pd.DataFrame({"facility_id": ["a", "b"]})},
    ])
    manifest = _load_manifest(run_dir)
    by_id = _stages_by_id(manifest)
    assert manifest["run_id"] == "T1"
    assert by_id["seeds"]["type"] == "input_data"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trace_helpers.py -q`
Expected: FAIL — `ModuleNotFoundError` / `ImportError` on `app.runtime.trace`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/runtime/trace.py
"""Positional provenance tracer: walk a claim's row back through the
row-preserving stages of one run, by row ordinal alone.

A stage is *row-preserving* when output row i is produced from input row i by
position — true for `input_data` (rows originate here) and `python_row_function`
(a 1:1 map over rows). For such a chain the row ordinal is the cross-stage key,
so nothing needs to be recorded: the tracer just reads row i at each stage. At
any other stage type the walk stops with a reason — `llm_transform` is 1:1 only
once PR #29 lands (issue #61); `join` / `aggregate` / `python_frame_function`
and fan-out reshape rows and need recorded edges (issue #58). The walk also
stops if a supposedly row-preserving hop has unequal row counts on its two
sides, because position cannot be trusted then.

Self-contained on the run directory: reads manifest.json (stage type, parent
edges, row counts) and outputs/<stage>.parquet (row values). It never reads the
compiled DAG, so it is unaffected by later edits to the methodology.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

ROW_PRESERVING: frozenset[str] = frozenset({"input_data", "python_row_function"})

# Why a stage could not be crossed, and the issue that tracks lifting the stop.
STOP_MESSAGES: dict[str, str] = {
    "origin": "input_data stage — the rows originate here",
    "llm_transform": "llm_transform is 1:1 only once PR #29 lands (issue #61)",
    "reshaping": "stage reshapes rows (fan-in/out) — row lineage is issue #58",
    "rowcount_mismatch": (
        "row counts differ across this hop, so position is not trustworthy — "
        "row lineage is issue #58"
    ),
    "missing_output": "this stage's output file is missing from the run",
    "missing_parent": "the parent named in the manifest is not in the run",
    "no_parent_edge": "the manifest records no input edge for this stage",
}


@dataclass
class StopReason:
    kind: str      # a key of STOP_MESSAGES
    stage_id: str  # the stage that could not be crossed (or the origin)
    message: str


@dataclass
class Hop:
    stage_id: str
    stage_type: str
    row_ordinal: int
    row: dict[str, Any]     # the row's cells, verbatim
    columns_new: list[str]  # columns first appearing at this stage vs its parent
    origin: str             # "source" | "computed" | "llm" | "other"


@dataclass
class Trace:
    run_id: str
    start_stage: str
    start_row: int
    hops: list[Hop]         # newest first: start stage, then each ancestor
    terminal: StopReason


def _load_manifest(run_dir: Path) -> dict[str, Any]:
    path = Path(run_dir) / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"no manifest.json in {run_dir}")
    return json.loads(path.read_text(encoding="utf-8"))


def _stages_by_id(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["stage_id"]: s for s in manifest.get("stages", [])}


def _parents(stage_record: dict[str, Any]) -> list[str]:
    parents: list[str] = []
    for entry in stage_record.get("input_validation") or []:
        phase = entry.get("phase", "")
        if phase.startswith("input:"):
            parents.append(phase.split(":", 1)[1])
    return parents


def _origin(stage_type: str) -> str:
    return {
        "input_data": "source",
        "python_row_function": "computed",
        "llm_transform": "llm",
    }.get(stage_type, "other")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_trace_helpers.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/runtime/trace.py tests/test_trace_helpers.py
git commit -m "feat(trace): tracer types, constants, manifest/edge readers"
```

---

### Task 2: Row and column-origin helpers

**Files:**
- Modify: `app/runtime/trace.py`
- Test: `tests/test_trace_columns.py`

**Interfaces:**
- Consumes: `write_run` from `tests/test_trace_helpers.py` (import it).
- Produces: `_read_output(run_dir: Path, stage_record: dict) -> pd.DataFrame | None`, `_row_dict(df: pd.DataFrame, r: int) -> dict[str, Any]`, `_new_columns(child: pd.DataFrame, parent: pd.DataFrame | None) -> list[str]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trace_columns.py
"""Unit tests for the per-row read and the column-origin diff."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.runtime.trace import _new_columns, _read_output, _row_dict
from tests.test_trace_helpers import write_run


def test_read_output_returns_none_when_file_missing(tmp_path):
    run_dir = write_run(tmp_path, [
        {"id": "seeds", "type": "input_data", "parents": [],
         "df": pd.DataFrame({"a": [1]})},
    ])
    (run_dir / "outputs" / "seeds.parquet").unlink()
    assert _read_output(run_dir, {"output_path": "outputs/seeds.parquet"}) is None
    assert _read_output(run_dir, {}) is None


def test_row_dict_stringifies_keys_and_delists_arrays():
    df = pd.DataFrame({"name": ["x"], "tags": [np.array(["p", "q"])]})
    assert _row_dict(df, 0) == {"name": "x", "tags": ["p", "q"]}


def test_new_columns_is_child_minus_parent():
    parent = pd.DataFrame({"facility_id": ["a"], "name": ["x"]})
    child = pd.DataFrame({"facility_id": ["a"], "name": ["x"], "score": [1]})
    assert _new_columns(child, parent) == ["score"]


def test_new_columns_all_when_no_parent():
    child = pd.DataFrame({"facility_id": ["a"], "name": ["x"]})
    assert _new_columns(child, None) == ["facility_id", "name"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trace_columns.py -q`
Expected: FAIL — `ImportError` on `_new_columns` / `_read_output` / `_row_dict`.

- [ ] **Step 3: Write minimal implementation**

Append to `app/runtime/trace.py`:

```python
def _read_output(run_dir: Path, stage_record: dict[str, Any]) -> pd.DataFrame | None:
    rel = stage_record.get("output_path")
    if not rel:
        return None
    path = Path(run_dir) / rel
    if not path.exists():
        return None
    return pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)


def _scalar(value: Any) -> Any:
    # Parquet list/array cells arrive as numpy arrays; make them plain lists so
    # the row is JSON-able. Leave everything else (including strings) untouched.
    if hasattr(value, "tolist") and not isinstance(value, str):
        return value.tolist()
    return value


def _row_dict(df: pd.DataFrame, r: int) -> dict[str, Any]:
    return {str(k): _scalar(v) for k, v in df.iloc[r].items()}


def _new_columns(child: pd.DataFrame, parent: pd.DataFrame | None) -> list[str]:
    if parent is None:
        return [str(c) for c in child.columns]
    parent_cols = {str(c) for c in parent.columns}
    return [str(c) for c in child.columns if str(c) not in parent_cols]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_trace_columns.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/runtime/trace.py tests/test_trace_columns.py
git commit -m "feat(trace): per-row read + column-origin diff helpers"
```

---

### Task 3: The positional walk — `trace_row`

**Files:**
- Modify: `app/runtime/trace.py`
- Test: `tests/test_trace_walk.py`

**Interfaces:**
- Consumes: everything from Tasks 1–2; `write_run`.
- Produces: `trace_row(run_dir: Path, stage_id: str, row_ordinal: int) -> Trace`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trace_walk.py
"""End-to-end tests for the positional walk: clean chains, every stop reason,
and the defensive guards."""
from __future__ import annotations

import pandas as pd
import pytest

from app.runtime.trace import trace_row
from tests.test_trace_helpers import write_run


def _chain(tmp_path, second_type: str):
    """A two-stage run: input_data 'seeds' -> `second_type` 'enrich', 3 rows,
    positional. 'enrich' adds a 'score' column."""
    seeds = pd.DataFrame({"facility_id": ["a", "b", "c"], "name": ["A", "B", "C"]})
    enrich = seeds.assign(score=[10, 20, 30])
    return write_run(tmp_path, [
        {"id": "seeds", "type": "input_data", "parents": [], "df": seeds},
        {"id": "enrich", "type": second_type, "parents": ["seeds"], "df": enrich},
    ])


def test_row_preserving_chain_traces_to_origin(tmp_path):
    run_dir = _chain(tmp_path, "python_row_function")
    trace = trace_row(run_dir, "enrich", 1)
    assert [h.stage_id for h in trace.hops] == ["enrich", "seeds"]
    assert [h.row_ordinal for h in trace.hops] == [1, 1]         # same ordinal
    assert trace.hops[0].row["name"] == "B"
    assert trace.hops[0].columns_new == ["score"]               # new at enrich
    assert trace.hops[0].origin == "computed"
    assert trace.hops[1].columns_new == ["facility_id", "name"]  # origin: all new
    assert trace.terminal.kind == "origin"


def test_stop_at_llm_transform_points_at_issue_61(tmp_path):
    run_dir = _chain(tmp_path, "llm_transform")
    trace = trace_row(run_dir, "enrich", 0)
    assert [h.stage_id for h in trace.hops] == ["enrich"]        # cannot cross
    assert trace.terminal.kind == "llm_transform"
    assert "#61" in trace.terminal.message


def test_stop_at_reshaping_stage_points_at_issue_58(tmp_path):
    run_dir = _chain(tmp_path, "python_frame_function")
    trace = trace_row(run_dir, "enrich", 0)
    assert [h.stage_id for h in trace.hops] == ["enrich"]
    assert trace.terminal.kind == "reshaping"
    assert "#58" in trace.terminal.message


def test_rowcount_mismatch_on_preserving_stage_stops_defensively(tmp_path):
    # 'enrich' declares python_row_function but emits fewer rows than its parent.
    seeds = pd.DataFrame({"facility_id": ["a", "b", "c"]})
    enrich = pd.DataFrame({"facility_id": ["a", "b"], "score": [1, 2]})
    run_dir = write_run(tmp_path, [
        {"id": "seeds", "type": "input_data", "parents": [], "df": seeds},
        {"id": "enrich", "type": "python_row_function", "parents": ["seeds"], "df": enrich},
    ])
    trace = trace_row(run_dir, "enrich", 0)
    assert [h.stage_id for h in trace.hops] == ["enrich"]
    assert trace.terminal.kind == "rowcount_mismatch"
    assert "#58" in trace.terminal.message


def test_row_out_of_range_raises(tmp_path):
    run_dir = _chain(tmp_path, "python_row_function")
    with pytest.raises(ValueError, match="out of range"):
        trace_row(run_dir, "enrich", 5)


def test_unknown_stage_raises(tmp_path):
    run_dir = _chain(tmp_path, "python_row_function")
    with pytest.raises(ValueError, match="not in run"):
        trace_row(run_dir, "nope", 0)


def test_missing_output_file_stops(tmp_path):
    run_dir = _chain(tmp_path, "python_row_function")
    (run_dir / "outputs" / "seeds.parquet").unlink()
    trace = trace_row(run_dir, "enrich", 0)
    # 'enrich' shows, but crossing into 'seeds' finds no file.
    assert [h.stage_id for h in trace.hops] == ["enrich"]
    assert trace.terminal.kind == "missing_output"
    assert trace.terminal.stage_id == "seeds"


def test_preserving_stage_with_multiple_parents_stops_as_reshaping(tmp_path):
    left = pd.DataFrame({"k": ["a", "b"]})
    right = pd.DataFrame({"k": ["a", "b"]})
    joined = pd.DataFrame({"k": ["a", "b"], "v": [1, 2]})
    run_dir = write_run(tmp_path, [
        {"id": "left", "type": "input_data", "parents": [], "df": left},
        {"id": "right", "type": "input_data", "parents": [], "df": right},
        # Mislabeled as row-preserving but has two parents: not positional.
        {"id": "j", "type": "python_row_function", "parents": ["left", "right"], "df": joined},
    ])
    trace = trace_row(run_dir, "j", 0)
    assert [h.stage_id for h in trace.hops] == ["j"]
    assert trace.terminal.kind == "reshaping"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trace_walk.py -q`
Expected: FAIL — `ImportError` on `trace_row`.

- [ ] **Step 3: Write minimal implementation**

Append to `app/runtime/trace.py`:

```python
def trace_row(run_dir: Path, stage_id: str, row_ordinal: int) -> Trace:
    """Trace one row's ancestry backward through row-preserving stages.

    Returns a `Trace` whose `hops` run newest-first from `(stage_id,
    row_ordinal)` to either an `input_data` origin or the first stage that
    cannot be crossed (`terminal`). Raises `ValueError` for an unknown stage or
    an out-of-range row — those are caller bugs, not traceable states.
    """
    run_dir = Path(run_dir)
    manifest = _load_manifest(run_dir)
    by_id = _stages_by_id(manifest)
    if stage_id not in by_id:
        raise ValueError(f"stage {stage_id!r} not in run {run_dir.name}")

    hops: list[Hop] = []
    sid, r = stage_id, row_ordinal
    terminal: StopReason | None = None

    while terminal is None:
        record = by_id[sid]
        stage_type = record.get("type", "")
        df = _read_output(run_dir, record)
        if df is None:
            terminal = StopReason("missing_output", sid, STOP_MESSAGES["missing_output"])
            break
        if r < 0 or r >= len(df):
            raise ValueError(
                f"row {r} out of range for stage {sid!r} ({len(df)} rows)"
            )

        parents = _parents(record)
        parent_df = None
        if len(parents) == 1 and parents[0] in by_id:
            parent_df = _read_output(run_dir, by_id[parents[0]])

        hops.append(Hop(
            stage_id=sid,
            stage_type=stage_type,
            row_ordinal=r,
            row=_row_dict(df, r),
            columns_new=_new_columns(df, parent_df),
            origin=_origin(stage_type),
        ))

        # Can we cross into the parent, keeping the same ordinal?
        if stage_type == "input_data":
            terminal = StopReason("origin", sid, STOP_MESSAGES["origin"])
        elif not parents:
            terminal = StopReason("no_parent_edge", sid, STOP_MESSAGES["no_parent_edge"])
        elif stage_type not in ROW_PRESERVING:
            kind = "llm_transform" if stage_type == "llm_transform" else "reshaping"
            terminal = StopReason(kind, sid, STOP_MESSAGES[kind])
        elif len(parents) != 1:
            # A row-preserving stage has exactly one input; more means the
            # manifest is mislabeled — treat as reshaping, don't guess a parent.
            terminal = StopReason("reshaping", sid, STOP_MESSAGES["reshaping"])
        else:
            parent_id = parents[0]
            if parent_id not in by_id:
                terminal = StopReason("missing_parent", sid, STOP_MESSAGES["missing_parent"])
            elif parent_df is None:
                terminal = StopReason("missing_output", parent_id, STOP_MESSAGES["missing_output"])
            elif len(parent_df) != len(df):
                terminal = StopReason("rowcount_mismatch", sid, STOP_MESSAGES["rowcount_mismatch"])
            else:
                sid, r = parent_id, r  # same ordinal — the whole point

    return Trace(
        run_id=manifest.get("run_id", run_dir.name),
        start_stage=stage_id,
        start_row=row_ordinal,
        hops=hops,
        terminal=terminal,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_trace_walk.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add app/runtime/trace.py tests/test_trace_walk.py
git commit -m "feat(trace): positional walk with honest stop boundaries"
```

---

### Task 4: JSON-able serialization

**Files:**
- Modify: `app/runtime/trace.py`
- Test: `tests/test_trace_serialize.py`

**Interfaces:**
- Consumes: `Trace` from Task 3; `write_run`.
- Produces: `trace_to_dict(trace: Trace) -> dict[str, Any]` (nested dict/list of JSON scalars; the endpoint and any template consume this, never the dataclasses directly).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trace_serialize.py
"""The trace serializes to a plain nested dict a JSON response can carry."""
from __future__ import annotations

import json

import pandas as pd

from app.runtime.trace import trace_row, trace_to_dict
from tests.test_trace_helpers import write_run


def test_trace_to_dict_is_json_roundtrippable(tmp_path):
    seeds = pd.DataFrame({"facility_id": ["a", "b"], "name": ["A", "B"]})
    enrich = seeds.assign(score=[1, 2])
    run_dir = write_run(tmp_path, [
        {"id": "seeds", "type": "input_data", "parents": [], "df": seeds},
        {"id": "enrich", "type": "python_row_function", "parents": ["seeds"], "df": enrich},
    ])
    payload = trace_to_dict(trace_row(run_dir, "enrich", 0))
    # Must survive a JSON round-trip unchanged.
    assert json.loads(json.dumps(payload)) == payload
    assert payload["terminal"]["kind"] == "origin"
    assert payload["hops"][0]["stage_id"] == "enrich"
    assert payload["hops"][0]["columns_new"] == ["score"]
    assert payload["hops"][0]["row"]["name"] == "A"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trace_serialize.py -q`
Expected: FAIL — `ImportError` on `trace_to_dict`.

- [ ] **Step 3: Write minimal implementation**

Append to `app/runtime/trace.py`:

```python
def trace_to_dict(trace: Trace) -> dict[str, Any]:
    """Flatten a Trace to a JSON-able nested dict for the API and templates."""
    return {
        "run_id": trace.run_id,
        "start_stage": trace.start_stage,
        "start_row": trace.start_row,
        "hops": [
            {
                "stage_id": hop.stage_id,
                "stage_type": hop.stage_type,
                "row_ordinal": hop.row_ordinal,
                "row": hop.row,
                "columns_new": hop.columns_new,
                "origin": hop.origin,
            }
            for hop in trace.hops
        ],
        "terminal": {
            "kind": trace.terminal.kind,
            "stage_id": trace.terminal.stage_id,
            "message": trace.terminal.message,
        },
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_trace_serialize.py -q`
Expected: PASS. If a `row` cell holds a numpy scalar that `json.dumps` rejects, extend `_scalar` in Task 2 to coerce numpy scalars via `.item()` — but pandas `.items()` already yields Python scalars for object/int/float columns, so the round-trip should pass as written.

- [ ] **Step 5: Commit**

```bash
git add app/runtime/trace.py tests/test_trace_serialize.py
git commit -m "feat(trace): JSON-able trace serialization"
```

**PR boundary:** Tasks 1–4 are a self-contained, fully tested tracer with no web coupling. This is the first mergeable PR. Task 5 wires it to an endpoint.

---

### Task 5: JSON endpoint on the run viewer

**Files:**
- Modify: `app/web/routers/runs.py`
- Test: `tests/test_trace_endpoint.py`

**Interfaces:**
- Consumes: `trace_row`, `trace_to_dict` from Task 3–4; `runs_dir`, `load_manifest` (already imported in `runs.py`).
- Produces: `GET /project/{project}/runs/{run_id}/stage/{stage_id}/row/{row}/trace` → `JSONResponse(trace_to_dict(...))`; 404 when the run or stage is absent, 400 when the row ordinal is out of range.

- [ ] **Step 1: Write the failing test**

```python
# tests/web/test_trace_endpoint.py
"""The trace endpoint returns the serialized trace, and maps tracer errors to
HTTP status codes. Uses a temp EXAMPLES_DIR so it needs no committed run."""
from __future__ import annotations

import pandas as pd
from fastapi.testclient import TestClient

import app.web.loading as loading
from app.main import app
from tests.test_trace_helpers import write_run


def _project_run(tmp_path, monkeypatch):
    project_runs = tmp_path / "proj" / "runs"
    project_runs.mkdir(parents=True)
    seeds = pd.DataFrame({"facility_id": ["a", "b"], "name": ["A", "B"]})
    enrich = seeds.assign(score=[1, 2])
    write_run(project_runs, [
        {"id": "seeds", "type": "input_data", "parents": [], "df": seeds},
        {"id": "enrich", "type": "python_row_function", "parents": ["seeds"], "df": enrich},
    ], run_id="R1")
    # runs_dir() resolves against loading.EXAMPLES_DIR; point it at our temp tree
    # (same pattern as tests/test_run_rows.py).
    monkeypatch.setattr(loading, "EXAMPLES_DIR", tmp_path)
    return TestClient(app)


def test_trace_endpoint_returns_serialized_trace(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)
    resp = client.get("/project/proj/runs/R1/stage/enrich/row/1/trace")
    assert resp.status_code == 200
    body = resp.json()
    assert [h["stage_id"] for h in body["hops"]] == ["enrich", "seeds"]
    assert body["terminal"]["kind"] == "origin"


def test_trace_endpoint_404_for_unknown_stage(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)
    resp = client.get("/project/proj/runs/R1/stage/nope/row/0/trace")
    assert resp.status_code == 404


def test_trace_endpoint_400_for_out_of_range_row(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)
    resp = client.get("/project/proj/runs/R1/stage/enrich/row/9/trace")
    assert resp.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/web/test_trace_endpoint.py -q`
Expected: FAIL — 404 from an unregistered route (no handler yet), so the first assertion (`== 200`) fails.

- [ ] **Step 3: Write minimal implementation**

Add to the imports in `app/web/routers/runs.py` (extend the existing `from app.web.loading import (...)` block with nothing new — `runs_dir`/`load_manifest` are already imported) and add:

```python
from app.runtime.trace import trace_row, trace_to_dict
```

Then add the route (place it after `run_stage_rows`):

```python
@router.get("/project/{project}/runs/{run_id}/stage/{stage_id}/row/{row}/trace")
async def run_stage_row_trace(project: str, run_id: str, stage_id: str, row: int):
    """Show-your-work for one output row: its ancestry through row-preserving
    stages, as JSON. 404 if the run/stage is absent, 400 if the row ordinal is
    out of range."""
    run_dir = runs_dir(project) / run_id
    load_manifest(run_dir)  # 404s if the run doesn't exist
    try:
        trace = trace_row(run_dir, stage_id, row)
    except ValueError as exc:
        detail = str(exc)
        if "not in run" in detail:
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail) from exc
    return JSONResponse(trace_to_dict(trace))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/web/test_trace_endpoint.py -q`
Expected: PASS (3 tests). The FastAPI app object is `from app.main import app` and the temp-runs monkeypatch is `loading.EXAMPLES_DIR` — both copied from the existing `tests/test_run_rows.py`.

- [ ] **Step 5: Run the whole suite and commit**

Run: `python -m pytest -q`
Expected: PASS (all prior tests plus the new ones).

```bash
git add app/web/routers/runs.py tests/web/test_trace_endpoint.py
git commit -m "feat(web): JSON show-your-work trace endpoint"
```

---

## Follow-ups (not in this plan)

- Reader-facing HTML hop-card rendering (dossier inline + run-viewer partial) — its own plan; consumes `trace_to_dict`.
- Flip `llm_transform` into `ROW_PRESERVING` once PR #29 lands — [issue #61](https://github.com/surveit/data_workflow/issues/61).
- Recorded-edge sidecars for row-reshaping stages — [issue #58](https://github.com/surveit/data_workflow/issues/58).

## Self-review notes

- **Spec coverage:** positional walk (§5), row-preserving stop boundary + defensive row-count guard (Global Constraints, Task 3), column-origin from data not schema (Task 2 — a deliberate simplification of design §6.2 item 1 that drops the #29 dependency), sidecar-free (design §4 scope revision), corner cases C3/C7-analog/C11 handled as stops or raw passthrough. Payload narrowing (§6.2 item 2), reference matching (§6.2 item 3), gap report (§6.2 item 5), and the HTML view (§6.3) are explicitly deferred to the follow-up view plan — they are display concerns over this engine.
- **No placeholders:** every step has complete code and an exact command.
- **Type consistency:** `Trace`/`Hop`/`StopReason` field names are used identically in Tasks 3–5; `trace_row`/`trace_to_dict`/`_new_columns`/`_read_output`/`_row_dict`/`_parents`/`_origin` names match across tasks.

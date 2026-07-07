# Runtime lineage substrate — Implementation Plan

> **⚠️ DEFERRED (2026-07-07) — this plan is not the active v1.** SYW v1 pivoted to
> **row-preserving positional tracing**, which needs no recording and no sidecars:
> for a chain of row-preserving stages (`input_data`, `python_row_function`,
> `llm_transform` once [#29](https://github.com/surveit/data_workflow/pull/29) makes
> it strictly 1:1 *and* order-preserving), output row *i* traces to input row *i* by
> position. This whole recorded-edge substrate is only needed to trace **across
> row-reshaping stages** (fan-in/out, `join`, `aggregate`, `python_frame_function`)
> and is captured as [issue #58](https://github.com/surveit/data_workflow/issues/58).
> The tasks below remain the worked-out design for that deferred work.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the runtime record, for every stage it executes, which input row(s) produced each output row — persisted as per-run sidecar files next to the existing stage outputs — so a later tracer and view can reconstruct a claim's provenance without guessing.

**Architecture:** Each stage handler already computes its input→output row mapping transiently while executing (an LLM fan-out loop, a dataframe merge, a group-by); today that mapping is discarded. This plan has the recording-capable handlers stash their edges into the run context dict (`ctx`, the same side-channel handlers already use for `queue_stats`/`llm_backend`), and has the runner persist those edges to `runs/<run_id>/lineage/<stage_id>.parquet` after it applies its own row slicing, so the persisted edges stay aligned with the persisted output rows. Handlers that genuinely cannot see their mapping (`python_frame_function`) and ones deferred for now (`human_review_queue`) simply record nothing; the absence of a sidecar is the signal that a stage is untracked. This plan is §4 of the design doc only — the tracer (§5) and view (§6) are separate follow-on plans.

**Tech Stack:** Python 3.12, pandas, pyarrow (parquet), pytest. No new dependencies.

## Global Constraints

- **Never fabricate; fail loudly.** No default/placeholder edges. A handler that cannot observe a mapping records nothing (→ untracked); it never emits a guessed edge. (Copied from the project cardinal rule.)
- **No `Any` leakage / no `# type: ignore`.** Type `ctx` as `dict[str, Any]` to match the existing handler signatures (they already import `Any`); everywhere else use concrete types. Never silence mypy.
- **Lineage lives in sidecar files, never as columns on the data tables.** A stage's `outputs/<id>.parquet` must stay byte-identical to what it is today. (Design §6.1 rule 1.)
- **Edges use the handler's pre-slice output ordinals; the runner re-aligns them** to the sliced-and-reindexed output before persisting. Output row ordinal = position in the persisted `outputs/<id>.parquet` (0-indexed). (Design §5, and the runner's offset/limit slice at `app/runtime/runner.py:346-357`.)
- **Edge schema is exactly three columns:** `out_row` (int), `in_stage` (str), `in_row` (int). One row per edge. (Design §4.1.)
- Tests run offline: the autouse `force_mock_llm` fixture in `tests/conftest.py` sets `CW_LLM_FORCE_MOCK=1`. Handler-level LLM tests monkeypatch `call_llm_batch` directly rather than relying on mock internals.

---

## File Structure

**New files:**
- `app/runtime/lineage.py` — the whole lineage vocabulary: the `Edge` type alias, the two `ctx` recording helpers (`record_edges`, `record_llm_columns`), the `positional_edges` builder for 1:1 stages, the `slice_edges` re-alignment function, and the `persist` sidecar writer. One module so every piece of "what a lineage edge is and how it's stored" lives together.
- `tests/test_lineage.py` — all lineage tests: pure-helper unit tests, one recording test per handler, and the two runner end-to-end/alignment tests.

**Modified files:**
- `app/runtime/stages/python_functions.py` — `handle_python_row_function` records 1:1 positional edges. `handle_python_frame_function` unchanged (opaque, records nothing).
- `app/runtime/stages/llm_transform.py` — record 1→N edges per input row and the set of LLM-written columns.
- `app/runtime/stages/join.py` — record left+right source edges through the merge.
- `app/runtime/stages/aggregate.py` — record group-member edges.
- `app/runtime/runner.py` — after the existing offset/limit slice, re-align edges from `ctx` and persist the sidecars.
- `app/runtime/AGENTS.md` — document the sidecar layout and which stage types record.
- `docs/superpowers/specs/2026-07-01-show-your-work-design.md` — flip §4's status line to "implemented".

**Not touched (record nothing, by decision):** `input_data` (origin — no parents), `python_frame_function` (opaque), `human_review_queue` (its real handler drops rejected rows and concatenates decided+passthrough, so it is not the clean 1:1 the design table assumed; deferred to a follow-up — see Task 7 note), `publish` (terminal; a claim's row is publish's *input* row, reached via the parent stage's edges).

---

## Task 1: Lineage module (pure helpers)

**Files:**
- Create: `app/runtime/lineage.py`
- Test: `tests/test_lineage.py`

**Interfaces:**
- Produces:
  - `Edge = tuple[int, str, int]` — `(out_row, in_stage, in_row)`.
  - `record_edges(ctx: dict[str, Any], stage_id: str, edges: list[Edge]) -> None`
  - `record_llm_columns(ctx: dict[str, Any], stage_id: str, columns: list[str]) -> None`
  - `positional_edges(in_stage: str, n_rows: int) -> list[Edge]`
  - `slice_edges(edges: list[Edge], offset: int | None, limit: int | None) -> list[Edge]`
  - `persist(run_dir: Path, stage_id: str, edges: list[Edge], llm_columns: list[str] | None = None) -> None`
  - `LINEAGE_COLUMNS = ["out_row", "in_stage", "in_row"]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_lineage.py`:

```python
"""Lineage substrate: pure helpers (this task), per-handler recording, and
end-to-end persistence/alignment (later tasks)."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from app.runtime import lineage


def test_positional_edges_is_one_to_one():
    assert lineage.positional_edges("load", 3) == [(0, "load", 0), (1, "load", 1), (2, "load", 2)]
    assert lineage.positional_edges("load", 0) == []


def test_slice_edges_offset_then_limit_matches_runner():
    # 5 output rows 0..4, each from the same-numbered input row. The runner's
    # offset=1 drops out_row 0; limit=3 keeps the next 3; survivors renumber to
    # 0,1,2 but keep pointing at original input rows 1,2,3.
    edges = [(i, "load", i) for i in range(5)]
    assert lineage.slice_edges(edges, offset=1, limit=3) == [(0, "load", 1), (1, "load", 2), (2, "load", 3)]


def test_slice_edges_none_is_noop():
    edges = [(0, "load", 0), (1, "load", 1)]
    assert lineage.slice_edges(edges, offset=None, limit=None) == edges


def test_slice_edges_keeps_all_edges_of_a_surviving_fanned_row():
    # A fan-in output row has several edges; slicing must keep or drop them together.
    edges = [(0, "a", 0), (0, "b", 5), (1, "a", 1)]
    assert lineage.slice_edges(edges, offset=1, limit=None) == [(0, "a", 1)]


def test_persist_writes_edges_and_llm_columns(tmp_path: Path):
    lineage.persist(tmp_path, "extract", [(0, "grep", 0), (1, "grep", 0)], llm_columns=["value", "unit"])
    got = pd.read_parquet(tmp_path / "lineage" / "extract.parquet")
    assert lineage.LINEAGE_COLUMNS == list(got.columns)
    assert list(got["out_row"]) == [0, 1]
    assert list(got["in_row"]) == [0, 0]
    cols = json.loads((tmp_path / "llm_columns" / "extract.json").read_text(encoding="utf-8"))
    assert cols == ["value", "unit"]


def test_persist_empty_edges_still_writes_a_file(tmp_path: Path):
    # A recording stage that produced zero output rows writes an empty sidecar,
    # so "no file" cleanly means "not a recording stage", never "0 rows".
    lineage.persist(tmp_path, "dbl", [])
    got = pd.read_parquet(tmp_path / "lineage" / "dbl.parquet")
    assert list(got.columns) == lineage.LINEAGE_COLUMNS
    assert len(got) == 0
    assert not (tmp_path / "llm_columns" / "dbl.json").exists()


def test_record_helpers_stash_into_ctx():
    ctx: dict = {}
    lineage.record_edges(ctx, "s", [(0, "p", 0)])
    lineage.record_llm_columns(ctx, "s", ["a"])
    assert ctx["lineage"]["s"] == [(0, "p", 0)]
    assert ctx["llm_columns"]["s"] == ["a"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_lineage.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.runtime.lineage'`.

- [ ] **Step 3: Write the module**

Create `app/runtime/lineage.py`:

```python
"""Row-level lineage: which input row(s) produced each output row of a stage.

Recording-capable handlers stash edges into the run context via `record_edges`
(and llm_transform adds `record_llm_columns`); the runner re-aligns them to the
sliced output and `persist`s them next to `outputs/<stage_id>.parquet`. An edge
is (out_row, in_stage, in_row) using output-row ordinals within the persisted
tables. The absence of a `lineage/<stage_id>.parquet` means the stage recorded
nothing (opaque or deferred) — never that it had zero rows: a recorded stage
with no output rows still writes an empty file.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

Edge = tuple[int, str, int]  # (out_row, in_stage, in_row)

LINEAGE_COLUMNS = ["out_row", "in_stage", "in_row"]


def record_edges(ctx: dict[str, Any], stage_id: str, edges: list[Edge]) -> None:
    """Stash a stage's lineage edges into the run context for the runner to
    slice and persist. Edges use the handler's pre-slice output ordinals."""
    ctx.setdefault("lineage", {})[stage_id] = edges


def record_llm_columns(ctx: dict[str, Any], stage_id: str, columns: list[str]) -> None:
    """Record which output columns an llm_transform stage's LLM wrote (as
    observed from the result dict keys), versus columns carried from the input."""
    ctx.setdefault("llm_columns", {})[stage_id] = list(columns)


def positional_edges(in_stage: str, n_rows: int) -> list[Edge]:
    """1:1 by position: output row i came from input row i of `in_stage`."""
    return [(i, in_stage, i) for i in range(n_rows)]


def slice_edges(edges: list[Edge], offset: int | None, limit: int | None) -> list[Edge]:
    """Re-align edges to the runner's output slice. The runner drops the first
    `offset` output rows then keeps the first `limit`; mirror that on out_row and
    renumber survivors so they index the persisted (sliced) output. in_row is
    never touched — it indexes the parent's own output, which this slice does not
    change."""
    drop = offset if isinstance(offset, int) and offset > 0 else 0
    out: list[Edge] = []
    for out_row, in_stage, in_row in edges:
        if out_row < drop:
            continue
        new_row = out_row - drop
        if isinstance(limit, int) and limit >= 0 and new_row >= limit:
            continue
        out.append((new_row, in_stage, in_row))
    return out


def persist(
    run_dir: Path, stage_id: str, edges: list[Edge], llm_columns: list[str] | None = None
) -> None:
    """Write `lineage/<stage_id>.parquet` (always, even for empty edges) and,
    when given, `llm_columns/<stage_id>.json`."""
    lineage_dir = run_dir / "lineage"
    lineage_dir.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(edges, columns=LINEAGE_COLUMNS)
    frame.to_parquet(lineage_dir / f"{stage_id}.parquet", index=False)
    if llm_columns is not None:
        cols_dir = run_dir / "llm_columns"
        cols_dir.mkdir(parents=True, exist_ok=True)
        (cols_dir / f"{stage_id}.json").write_text(
            json.dumps(llm_columns), encoding="utf-8"
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_lineage.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add app/runtime/lineage.py tests/test_lineage.py
git commit -m "feat(lineage): edge type, ctx recorders, slice + persist helpers"
```

---

## Task 2: python_row_function records 1:1 edges

**Files:**
- Modify: `app/runtime/stages/python_functions.py`
- Test: `tests/test_lineage.py`

**Interfaces:**
- Consumes: `lineage.record_edges`, `lineage.positional_edges` (Task 1).
- Produces: after `handle_python_row_function` runs, `ctx["lineage"][stage.id]` holds `[(0, in, 0), (1, in, 1), ...]` for the single input `in`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lineage.py`:

```python
from app.models import Stage
from app.runtime.stages import handle_python_row_function


def _row_stage(code: str) -> Stage:
    return Stage.model_validate({
        "id": "dbl", "name": "dbl", "type": "python_row_function",
        "inputs": [{"id": "src"}],
        "function": {"kind": "inline", "code": code},
    })


def test_row_function_records_positional_edges():
    df = pd.DataFrame({"x": [1, 2, 3]})
    ctx: dict = {}
    handle_python_row_function(
        _row_stage("def transform(row):\n    return {'x': row['x'], 'y': row['x'] * 10}\n"),
        {"src": df}, ctx,
    )
    assert ctx["lineage"]["dbl"] == [(0, "src", 0), (1, "src", 1), (2, "src", 2)]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_lineage.py::test_row_function_records_positional_edges -v`
Expected: FAIL — `KeyError: 'lineage'`.

- [ ] **Step 3: Add recording to the handler**

In `app/runtime/stages/python_functions.py`, add the import near the top (after the existing imports):

```python
from ..lineage import positional_edges, record_edges
```

In `handle_python_row_function`, replace the final two lines:

```python
        out_rows.append(result)
    return pd.DataFrame(out_rows)
```

with:

```python
        out_rows.append(result)
    record_edges(ctx, stage.id, positional_edges(declared[0].id, len(out_rows)))
    return pd.DataFrame(out_rows)
```

(`handle_python_frame_function` is left unchanged — it is opaque and records nothing.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_lineage.py -v`
Expected: PASS. Also run `python -m pytest tests/test_row_function.py -v` — still PASS (unchanged output behavior).

- [ ] **Step 5: Commit**

```bash
git add app/runtime/stages/python_functions.py tests/test_lineage.py
git commit -m "feat(lineage): python_row_function records 1:1 edges"
```

---

## Task 3: Runner persists sidecars, re-aligned to the output slice

**Files:**
- Modify: `app/runtime/runner.py` (in `_execute_stages`, the per-stage block around lines 358-379)
- Test: `tests/test_lineage.py`

**Interfaces:**
- Consumes: `lineage.slice_edges`, `lineage.persist` (Task 1); `ctx["lineage"]`/`ctx["llm_columns"]` populated by recording handlers (Task 2 onward); the runner locals `offset` and `limit` (resolved at `runner.py:346` and `:352`) and `run_dir`, `sid`, `output`.
- Produces: `runs/<run_id>/lineage/<stage_id>.parquet` for every recording stage, aligned to that stage's persisted output; `runs/<run_id>/llm_columns/<stage_id>.json` for llm_transform stages.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_lineage.py`:

```python
from app.runtime.runner import execute_run
from app.services.versioning import create_version


def _seed_version(root: Path) -> str:
    return create_version(root, message="test seed", reviewer="test")["id"]


def _rowfn_project(root: Path) -> None:
    (root / "compiled").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    pd.DataFrame({"x": list(range(5))}).to_csv(root / "data" / "items.csv", index=False)
    load = {"id": "load", "name": "Load", "type": "input_data",
            "connector": {"kind": "file", "params": {"path": "data/items.csv", "format": "csv"}}}
    dbl = {"id": "dbl", "name": "Double", "type": "python_row_function",
           "inputs": [{"id": "load"}],
           "function": {"kind": "inline",
                        "code": "def transform(row):\n    return {'x': row['x'], 'y': row['x'] * 10}\n"}}
    (root / "compiled" / "01_load.json").write_text(json.dumps(load), encoding="utf-8")
    (root / "compiled" / "02_dbl.json").write_text(json.dumps(dbl), encoding="utf-8")


def test_lineage_sidecar_written_end_to_end(tmp_path: Path):
    _rowfn_project(tmp_path)
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, repo_root=tmp_path)
    run_dir = tmp_path / "runs" / manifest["run_id"]

    lin = pd.read_parquet(run_dir / "lineage" / "dbl.parquet")
    assert list(lin["out_row"]) == [0, 1, 2, 3, 4]
    assert list(lin["in_stage"]) == ["load"] * 5
    assert list(lin["in_row"]) == [0, 1, 2, 3, 4]
    # input_data has no parents → no sidecar (untracked-by-origin, not by gap).
    assert not (run_dir / "lineage" / "load.parquet").exists()


def test_lineage_realigns_with_offset_and_limit(tmp_path: Path):
    # offset=1 drops output row 0; limit=3 keeps the next three. The persisted
    # output is rows for x in [1,2,3]; lineage out_row renumbers to 0,1,2 but
    # in_row still points at the original input rows 1,2,3.
    _rowfn_project(tmp_path)
    _seed_version(tmp_path)
    manifest = execute_run(tmp_path, repo_root=tmp_path, limits={"dbl": 3}, offsets={"dbl": 1})
    run_dir = tmp_path / "runs" / manifest["run_id"]

    out = pd.read_parquet(run_dir / "outputs" / "dbl.parquet")
    lin = pd.read_parquet(run_dir / "lineage" / "dbl.parquet")
    assert list(out["x"]) == [1, 2, 3]
    assert list(lin["out_row"]) == [0, 1, 2]
    assert list(lin["in_row"]) == [1, 2, 3]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_lineage.py::test_lineage_sidecar_written_end_to_end tests/test_lineage.py::test_lineage_realigns_with_offset_and_limit -v`
Expected: FAIL — `FileNotFoundError` on `lineage/dbl.parquet` (runner does not persist yet).

- [ ] **Step 3: Wire persistence into the runner**

In `app/runtime/runner.py`, add the import with the other `.stages`/local imports near the top (after `from .stages import HANDLERS, HaltForReview` at line 33):

```python
from . import lineage
```

Then, in `_execute_stages`, locate this existing block (around lines 374-379):

```python
            outputs_so_far[sid] = output
            record["status"] = "ok" if out_rep.ok and all(
                v["ok"] for v in record["input_validation"]
            ) else "validation_warnings"
            record["rows"] = int(len(output))
            record["output_path"] = str(output_path.relative_to(run_dir))
```

Insert the lineage persistence immediately after `outputs_so_far[sid] = output`, so the block becomes:

```python
            outputs_so_far[sid] = output

            # Persist lineage sidecars for stages that recorded edges, re-aligned
            # to the same offset/limit slice just applied to `output` so out_row
            # ordinals index the persisted table. Stages that recorded nothing
            # (opaque frame functions, deferred queue, terminal publish) get no
            # sidecar — the tracer reads that absence as "untracked".
            recorded = (ctx.get("lineage") or {}).get(sid)
            if recorded is not None:
                lineage.persist(
                    run_dir, sid,
                    lineage.slice_edges(recorded, offset, limit),
                    (ctx.get("llm_columns") or {}).get(sid),
                )

            record["status"] = "ok" if out_rep.ok and all(
                v["ok"] for v in record["input_validation"]
            ) else "validation_warnings"
            record["rows"] = int(len(output))
            record["output_path"] = str(output_path.relative_to(run_dir))
```

(`offset` and `limit` are the locals resolved at lines 346 and 352; `limit` may be `None`, `slice_edges` handles that.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_lineage.py -v`
Expected: PASS. Run `python -m pytest tests/test_runner.py -v` — still PASS (outputs unchanged; only new sidecars added).

- [ ] **Step 5: Commit**

```bash
git add app/runtime/runner.py tests/test_lineage.py
git commit -m "feat(lineage): runner persists edge sidecars aligned to output slice"
```

---

## Task 4: llm_transform records fan-out edges and LLM-written columns

**Files:**
- Modify: `app/runtime/stages/llm_transform.py`
- Test: `tests/test_lineage.py`

**Interfaces:**
- Consumes: `lineage.record_edges`, `lineage.record_llm_columns` (Task 1).
- Produces: after `handle_llm_transform` runs, `ctx["lineage"][stage.id]` holds one edge per output row (a list result of length N yields N edges all pointing at the same input row); `ctx["llm_columns"][stage.id]` is the sorted union of the LLM result dict keys.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lineage.py`:

```python
import app.runtime.stages.llm_transform as llm_mod
from app.runtime.stages import handle_llm_transform


def _llm_stage() -> Stage:
    return Stage.model_validate({
        "id": "extract", "name": "extract", "type": "llm_transform",
        "inputs": [{"id": "src"}],
        "llm": {"prompt_template": "do {facility_id}"},
    })


def test_llm_transform_records_fanout_edges_and_columns(monkeypatch):
    # src row 0 fans out to two field entries; src row 1 yields one.
    def fake_batch(stage_id, llm, row_dicts):
        return [[{"field": "cpo", "value": "1"}, {"field": "ffb", "value": "2"}],
                [{"field": "oer", "value": "3"}]]
    monkeypatch.setattr(llm_mod, "call_llm_batch", fake_batch)

    src = pd.DataFrame({"facility_id": ["f0", "f1"]})
    ctx: dict = {}
    handle_llm_transform(_llm_stage(), {"src": src}, ctx)

    assert ctx["lineage"]["extract"] == [(0, "src", 0), (1, "src", 0), (2, "src", 1)]
    assert ctx["llm_columns"]["extract"] == ["field", "value"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_lineage.py::test_llm_transform_records_fanout_edges_and_columns -v`
Expected: FAIL — `KeyError: 'lineage'`.

- [ ] **Step 3: Add recording to the handler**

In `app/runtime/stages/llm_transform.py`, add the import after the existing `from ..llm import ...` line:

```python
from ..lineage import record_edges, record_llm_columns
```

Replace the fan-out loop (the current lines 28-38):

```python
    for row_dict, result in zip(row_dicts, results):
        if isinstance(result, list):
            for idx, item in enumerate(result):
                merged = {**row_dict, **(item if isinstance(item, dict) else {"_value": item})}
                merged["evidence_id"] = _evidence_id_for(row_dict, idx)
                out_rows.append(merged)
        elif isinstance(result, dict):
            merged = {**row_dict, **result}
            out_rows.append(merged)
        else:
            out_rows.append({**row_dict, "_raw": str(result)})
```

with a version that records one edge per appended output row and accumulates the LLM-written column names:

```python
    in_stage = stage.inputs[0].id
    edges: list[tuple[int, str, int]] = []
    llm_columns: set[str] = set()
    for in_row, (row_dict, result) in enumerate(zip(row_dicts, results)):
        if isinstance(result, list):
            for idx, item in enumerate(result):
                item_dict = item if isinstance(item, dict) else {"_value": item}
                merged = {**row_dict, **item_dict}
                merged["evidence_id"] = _evidence_id_for(row_dict, idx)
                out_rows.append(merged)
                edges.append((len(out_rows) - 1, in_stage, in_row))
                llm_columns.update(item_dict.keys())
        elif isinstance(result, dict):
            merged = {**row_dict, **result}
            out_rows.append(merged)
            edges.append((len(out_rows) - 1, in_stage, in_row))
            llm_columns.update(result.keys())
        else:
            out_rows.append({**row_dict, "_raw": str(result)})
            edges.append((len(out_rows) - 1, in_stage, in_row))
            llm_columns.add("_raw")
    record_edges(ctx, stage.id, edges)
    record_llm_columns(ctx, stage.id, sorted(llm_columns))
```

Leave the rest of the handler (the `pd.DataFrame(out_rows)` build and the column projection to `output_schema`) unchanged — projection drops columns, never rows, so the recorded `out_row` ordinals stay valid. `evidence_id` is runtime-synthesized (not an LLM value and not carried from input), so it is deliberately excluded from `llm_columns`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_lineage.py -v`
Expected: PASS. Run `python -m pytest tests/test_llm_json.py tests/test_llm_backend.py -v` — still PASS.

- [ ] **Step 5: Commit**

```bash
git add app/runtime/stages/llm_transform.py tests/test_lineage.py
git commit -m "feat(lineage): llm_transform records fan-out edges + llm columns"
```

---

## Task 5: join records left+right source edges

**Files:**
- Modify: `app/runtime/stages/join.py`
- Test: `tests/test_lineage.py`

**Interfaces:**
- Consumes: `lineage.record_edges` (Task 1).
- Produces: after `handle_join` runs, `ctx["lineage"][stage.id]` holds, per output row, one edge to the contributing left row and one to the contributing right row (an unmatched side in an outer join contributes no edge).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lineage.py`:

```python
from app.runtime.stages import handle_join


def _join_stage() -> Stage:
    return Stage.model_validate({
        "id": "cx", "name": "cx", "type": "join",
        "inputs": [{"id": "L"}, {"id": "R"}],
        "join": {"type": "inner", "keys": [{"left": "k", "right": "k"}]},
    })


def test_join_records_left_and_right_edges():
    left = pd.DataFrame({"k": ["a", "b"], "lv": [1, 2]})
    right = pd.DataFrame({"k": ["b", "a"], "rv": [9, 8]})
    ctx: dict = {}
    out = handle_join(_join_stage(), {"L": left, "R": right}, ctx)

    # inner join on k: one output row per matched pair.
    edges = ctx["lineage"]["cx"]
    # Each output row has exactly two edges (one L, one R); no temp columns leak.
    assert "__lin_row__" not in out.columns and "__rin_row__" not in out.columns
    by_out: dict[int, dict[str, int]] = {}
    for out_row, in_stage, in_row in edges:
        by_out.setdefault(out_row, {})[in_stage] = in_row
    # row with k="a": left row 0, right row 1; k="b": left row 1, right row 0.
    ks = list(out["k"])
    for out_row, k in enumerate(ks):
        if k == "a":
            assert by_out[out_row] == {"L": 0, "R": 1}
        else:
            assert by_out[out_row] == {"L": 1, "R": 0}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_lineage.py::test_join_records_left_and_right_edges -v`
Expected: FAIL — `KeyError: 'lineage'`.

- [ ] **Step 3: Add recording to the handler**

In `app/runtime/stages/join.py`, add the import after the existing imports:

```python
from ..lineage import record_edges
```

Replace the merge-and-select tail (current lines 34-40):

```python
    merged = left.merge(right, left_on=left_keys, right_on=right_keys, how=how, suffixes=("", "_r"))

    select = join_cfg.select
    if select:
        existing = [c for c in select if c in merged.columns]
        merged = merged[existing]
    return merged
```

with a version that tags each side's source ordinal, reads the edges off the merged result, then removes the tags before any column selection:

```python
    # Tag each side's source-row ordinal so the merge carries them through; read
    # them back per output row to record lineage, then drop them so the output
    # table is unchanged. Names are unlikely to collide with real columns.
    left = left.reset_index(drop=True).copy()
    right = right.reset_index(drop=True).copy()
    left["__lin_row__"] = range(len(left))
    right["__rin_row__"] = range(len(right))

    merged = left.merge(right, left_on=left_keys, right_on=right_keys, how=how, suffixes=("", "_r"))
    merged = merged.reset_index(drop=True)

    left_stage = stage.inputs[0].id
    right_stage = stage.inputs[1].id
    edges: list[tuple[int, str, int]] = []
    for out_row in range(len(merged)):
        lidx = merged.at[out_row, "__lin_row__"]
        ridx = merged.at[out_row, "__rin_row__"]
        if pd.notna(lidx):
            edges.append((out_row, left_stage, int(lidx)))
        if pd.notna(ridx):
            edges.append((out_row, right_stage, int(ridx)))
    record_edges(ctx, stage.id, edges)

    merged = merged.drop(columns=["__lin_row__", "__rin_row__"])

    select = join_cfg.select
    if select:
        existing = [c for c in select if c in merged.columns]
        merged = merged[existing]
    return merged
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_lineage.py -v`
Expected: PASS. Run `python -m pytest tests/test_stage.py -v` — still PASS.

- [ ] **Step 5: Commit**

```bash
git add app/runtime/stages/join.py tests/test_lineage.py
git commit -m "feat(lineage): join records left+right source edges"
```

---

## Task 6: aggregate records group-member edges

**Files:**
- Modify: `app/runtime/stages/aggregate.py`
- Test: `tests/test_lineage.py`

**Interfaces:**
- Consumes: `lineage.record_edges` (Task 1).
- Produces: after `handle_aggregate` runs, `ctx["lineage"][stage.id]` holds, per output group row, one edge to every input row sharing that group's `group_by` values.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lineage.py`:

```python
from app.runtime.stages import handle_aggregate


def _aggregate_stage() -> Stage:
    return Stage.model_validate({
        "id": "cov", "name": "cov", "type": "aggregate",
        "inputs": [{"id": "src"}],
        "aggregate": {"group_by": ["country"],
                      "aggregations": [{"output_column": "n", "formula": "count"}]},
    })


def test_aggregate_records_group_member_edges():
    src = pd.DataFrame({"country": ["ID", "ID", "MY"], "v": [1, 2, 3]})
    ctx: dict = {}
    out = handle_aggregate(_aggregate_stage(), {"src": src}, ctx)

    # Map each output group row to the set of input rows it drew from.
    members: dict[str, set[int]] = {}
    for out_row, in_stage, in_row in ctx["lineage"]["cov"]:
        assert in_stage == "src"
        members.setdefault(out["country"][out_row], set()).add(in_row)
    assert members == {"ID": {0, 1}, "MY": {2}}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_lineage.py::test_aggregate_records_group_member_edges -v`
Expected: FAIL — `KeyError: 'lineage'`.

- [ ] **Step 3: Add recording to the handler**

In `app/runtime/stages/aggregate.py`, add the import after the existing imports:

```python
from ..lineage import record_edges
```

At the end of `handle_aggregate`, replace the final `return` (current line 50):

```python
    return results if results is not None else pd.DataFrame(columns=group_by)
```

with a version that records group membership before returning. A group row's ancestors are all input rows sharing its `group_by` values (the per-aggregation `where` filters change which rows feed a given *formula*, not which rows belong to the group):

```python
    results = results if results is not None else pd.DataFrame(columns=group_by)

    # An output group row's ancestors are every input row with matching group_by
    # values. Build group -> member input ordinals, normalizing NaN to a single
    # sentinel so missing keys group together (matching groupby(dropna=False)).
    src = df.reset_index(drop=True)

    def _key(values: list[Any]) -> tuple[Any, ...]:
        return tuple(None if pd.isna(v) else v for v in values)

    members: dict[tuple[Any, ...], list[int]] = {}
    for in_row in range(len(src)):
        members.setdefault(_key([src.at[in_row, c] for c in group_by]), []).append(in_row)

    in_stage = stage.inputs[0].id
    edges: list[tuple[int, str, int]] = []
    for out_row in range(len(results)):
        key = _key([results.at[out_row, c] for c in group_by])
        for in_row in members.get(key, []):
            edges.append((out_row, in_stage, in_row))
    record_edges(ctx, stage.id, edges)

    return results
```

Add `Any` to the `typing` import at the top of the file (change `from typing import Any` — it is already imported; confirm and leave as-is if present).

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_lineage.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/runtime/stages/aggregate.py tests/test_lineage.py
git commit -m "feat(lineage): aggregate records group-member edges"
```

---

## Task 7: Document the sidecar layout and mark the spec

**Files:**
- Modify: `app/runtime/AGENTS.md`
- Modify: `docs/superpowers/specs/2026-07-01-show-your-work-design.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: Add a lineage section to the runtime AGENTS.md**

Open `app/runtime/AGENTS.md` and add this section after the run-layout description (adapt heading depth to the file's existing style):

```markdown
## Lineage sidecars

Alongside `outputs/<stage_id>.parquet`, a run records row-level lineage for the
stage types whose input→output mapping the runtime can observe:

- `lineage/<stage_id>.parquet` — columns `out_row, in_stage, in_row`, one row per
  edge. `out_row`/`in_row` index the persisted output tables (0-based). Recorded
  by `python_row_function` (1:1), `llm_transform` (1→N per input row), `join`
  (one edge to each side), and `aggregate` (one edge per group member).
- `llm_columns/<stage_id>.json` — for `llm_transform` only: the output columns the
  LLM wrote (result-dict keys), versus columns carried from the input row.

A stage with **no** `lineage/` file recorded nothing: `input_data` originates rows
(no parents), `python_frame_function` is opaque, `human_review_queue` is deferred,
and `publish` is terminal. Edges are captured by handlers into the run context and
persisted by the runner after its offset/limit slice, so they stay aligned with the
output tables. `app/runtime/lineage.py` holds the helpers.
```

- [ ] **Step 2: Flip the spec status line**

In `docs/superpowers/specs/2026-07-01-show-your-work-design.md`, change the §4 heading line:

```markdown
## 4. Lineage tracking (the new runtime capability — the core of this design)
```

to:

```markdown
## 4. Lineage tracking (the new runtime capability — the core of this design) — IMPLEMENTED
```

and add, directly under that heading, one line:

```markdown
> Implemented on `syw-lineage` (this plan). `human_review_queue` recording is
> deferred (its handler drops rejected rows and reorders, so it is not the clean
> 1:1 the §3 table assumed); it records nothing and reads as `untracked` for now.
```

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (the pre-existing 167 tests plus the new lineage tests).

- [ ] **Step 4: Commit and push**

```bash
git add app/runtime/AGENTS.md docs/superpowers/specs/2026-07-01-show-your-work-design.md
git commit -m "docs(lineage): sidecar layout in runtime AGENTS.md; mark spec §4 implemented"
git push origin syw-lineage
```

---

## Self-Review

**Spec coverage (design §4):**
- §4.1 recorded edges (`out_row/in_stage/in_row` sidecar) → Tasks 1, 3, and per-handler 2/4/5/6. ✓
- §4.1 `llm_columns` per llm_transform → Task 4. ✓
- §4.1 "labeled `recorded`" → the sidecar's existence *is* the recorded label; the `recovered`/`untracked` labels are the tracer's job (§4.2/§4.3, next plan), not §4 recording. Noted, not in scope. ✓
- §4.2 recovered edges (frame-function fallback) → explicitly deferred to the tracer plan (recovery is "after the fact", tracer-side per the design). Not a §4 recording gap. ✓
- §4.3 lineage not derived from primary_key / transform internals / LLM-written values → honored: recording is purely from execution structure; `llm_columns` records LLM-written columns but never treats their *values* as edges. ✓
- Offset/limit alignment (design §5 + runner behavior) → Task 1 `slice_edges` + Task 3 alignment test. ✓
- Sidecar-not-columns (design §6.1 rule 1) → Task 1 `persist` writes a separate relation; join/llm handlers drop temp columns so outputs are unchanged; asserted in Task 5. ✓

**Deferred within §4, documented:** `human_review_queue` recording (Task 7 note + spec line). This is the one §3-table entry not implemented; called out loudly rather than silently skipped.

**Placeholder scan:** no TBD/TODO; every code step shows complete code; every test shows real assertions. ✓

**Type consistency:** `Edge = tuple[int, str, int]` used identically in `lineage.py` and every handler's local `edges: list[tuple[int, str, int]]`. `record_edges(ctx, stage_id, edges)` / `record_llm_columns(ctx, stage_id, columns)` / `positional_edges(in_stage, n_rows)` / `slice_edges(edges, offset, limit)` / `persist(run_dir, stage_id, edges, llm_columns=None)` signatures match between Task 1 and all call sites. `ctx["lineage"][sid]` and `ctx["llm_columns"][sid]` keys consistent across handlers, runner, and tests. ✓

## Follow-on plans (not this plan)
- **Tracer (design §5, §4.2 recovery, §4.3 labels):** the `(run_id, stage_id, out_row) → subgraph` walk over the sidecars, recovered-edge matching for frame functions, and `recorded`/`recovered`/`untracked` labeling.
- **View (design §6):** run-viewer per-row route and the static dossier renderer + gap report (palm-side, off `palm-on-master`).

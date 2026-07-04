"""
compilation.py — the COMPILATION-OBJECT lifecycle (a service).

The compile MECHANISM (prose → draft DAG) lives in `app.compiler`. This service
owns everything around persisting one compile as a first-class object on disk,
so a web app can create it, poll it, list the index, and load a detail view —
paralleling how `app.services.versioning` / `app.services.node_review` own their
object lifecycles, and how `app.runtime.runner` owns a RUN.

A compilation lives at `<COMPILATIONS_ROOT>/<compilation_id>/` and holds:
  - manifest.json      — "what compiled, ok/invalid/error", polled while running
  - what_happened.json — the input excerpt + LLM prompt + raw response (audit)
  - dag/               — the DAG output (compiled/NN_<id>.json + methodology_raw.md)

Storage-root convention: compilations live under `COMPILATIONS_ROOT` (below). The
web/CLI callers default to it; tests pass a tmp dir. This mirrors the runner
deriving `methodology_dir / "runs"` by convention rather than threading a root
literal through every signature.
"""

from __future__ import annotations

import json
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from app.compiler import compile_methodology, read_input

# The default root every compilation object hangs off. Callers may override
# (tests, alternate layouts); the convention lives here, defined once.
COMPILATIONS_ROOT = Path("compilations")

# How much of the raw input to echo into what_happened.json for the object view.
_INPUT_EXCERPT_CHARS = 4000


# ─────────────────────────────────────────────────────────────────────────────
# DAG output — compiled/NN_<id>.json + methodology_raw.md (+ audit json)
# ─────────────────────────────────────────────────────────────────────────────

def write_methodology(result: dict[str, Any], out_dir: str | Path) -> dict[str, Any]:
    """Write the compiled DAG to a folder shaped like a methodology artifact:
      <out_dir>/compiled/NN_<id>.json   (one per stage, in order)
      <out_dir>/methodology_raw.md
      <out_dir>/compiler_result.json    (raw alongside cooked: full result, audit)
    Returns a manifest of written paths.

    Stages are written as JSON — the on-disk format the loader
    (app.services.loader) reads. The compiler emits raw draft dicts (which may
    be invalid; the manifest records that), so they are dumped as-is rather than
    round-tripped through the typed Stage model."""
    out_dir = Path(out_dir)
    compiled = out_dir / "compiled"
    compiled.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for i, stage in enumerate(result["stages"], start=1):
        sid = stage.get("id") or f"stage{i}"
        fname = f"{i:02d}_{sid}.json"
        fpath = compiled / fname
        fpath.write_text(
            json.dumps(stage, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        written.append(str(fpath))

    raw_md = out_dir / "methodology_raw.md"
    raw_md.write_text(result.get("methodology_raw") or "", encoding="utf-8")

    # Raw-alongside-cooked: persist the full result (minus the bulky prompt echo)
    # so the compile is auditable and re-sliceable.
    audit = {
        "name": result.get("name"),
        "compiler_notes": result.get("compiler_notes"),
        "validation": result.get("validation"),
        "stages": result.get("stages"),
    }
    audit_path = out_dir / "compiler_result.json"
    audit_path.write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "out_dir": str(out_dir),
        "stage_files": written,
        "methodology_raw": str(raw_md),
        "audit": str(audit_path),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Compilation object — persist a compile as a first-class object (parallels a RUN)
# ─────────────────────────────────────────────────────────────────────────────

def _stage_summary(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact [{id, type}] list for the manifest (parallels a run's stage list)."""
    out: list[dict[str, Any]] = []
    for i, s in enumerate(stages, start=1):
        out.append({"id": s.get("id") or f"stage{i}", "type": s.get("type", "?")})
    return out


def prepare_compilation(
    input_path: str | Path,
    name: str,
    model: str = "sonnet",
    compilations_root: str | Path = COMPILATIONS_ROOT,
) -> dict[str, Any]:
    """Create the compilation dir + id and write an initial `running` manifest so
    a caller can redirect to the compilation page immediately and poll it while
    the (multi-minute) compile proceeds in the background. Mirrors runner.prepare_run.

    Returns {compilation_id, comp_dir, input_path, name, model}."""
    compilations_root = Path(compilations_root)
    compilation_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    comp_dir = compilations_root / compilation_id
    comp_dir.mkdir(parents=True, exist_ok=True)

    input_path = Path(input_path)
    # A display hint only — every input is compiled as prose regardless.
    input_kind = "transcript" if input_path.suffix == ".jsonl" else "prose"

    manifest = {
        "compilation_id": compilation_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "name": name,
        "input": str(input_path),
        "input_kind": input_kind,
        "model": model,
        "status": "running",
        "n_stages": 0,
        "validation_issues": [],
        "stage_summary": [],
        "error": None,
    }
    (comp_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return {
        "compilation_id": compilation_id,
        "comp_dir": comp_dir,
        "input_path": str(input_path),
        "name": name,
        "model": model,
    }


def run_prepared_compilation(prep: dict[str, Any]) -> str:
    """Execute a compilation previously set up by prepare_compilation(). Suitable
    for running in a background thread — the manifest on disk is rewritten to its
    terminal state (ok | invalid | error) when done, and the what_happened.json +
    DAG output are written alongside.

    The compile itself can fail honestly (bad JSON from the model, an exception in
    parsing): in that case status is `error` and the manifest records the reason —
    we never write a fake-success object. A clean compile with schema issues is
    `invalid`; a clean compile that validates is `ok`.

    Returns the compilation_id."""
    comp_dir: Path = Path(prep["comp_dir"])
    compilation_id: str = prep["compilation_id"]
    input_path: str = prep["input_path"]
    name: str = prep["name"]
    model: str = prep["model"]

    manifest = json.loads((comp_dir / "manifest.json").read_text(encoding="utf-8"))

    try:
        input_text = read_input(input_path)
        result = compile_methodology(input_text, name, model=model)
    except Exception as exc:  # the compile failed — record it honestly, don't fake
        manifest["status"] = "error"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
        (comp_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        # Persist the traceback alongside so the failure is auditable.
        (comp_dir / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        return compilation_id

    stages = result["stages"]
    issues = result["validation"]

    # ── DAG output: compiled/NN_<id>.json + methodology_raw.md (+ audit json) ──
    dag_dir = comp_dir / "dag"
    write_methodology(result, dag_dir)

    # ── what_happened.json: the input excerpt, the prompt sent, raw response ──
    what_happened = {
        "input": input_path,
        "input_chars": len(input_text),
        "input_excerpt": input_text[:_INPUT_EXCERPT_CHARS],
        "input_truncated_in_excerpt": len(input_text) > _INPUT_EXCERPT_CHARS,
        "prompt": result.get("prompt"),
        "raw_llm_response": result.get("raw_llm"),
        "compiler_notes": result.get("compiler_notes"),
    }
    (comp_dir / "what_happened.json").write_text(
        json.dumps(what_happened, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ── manifest → terminal state ──
    manifest["status"] = "invalid" if issues else "ok"
    manifest["n_stages"] = len(stages)
    manifest["validation_issues"] = issues
    manifest["stage_summary"] = _stage_summary(stages)
    manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
    (comp_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return compilation_id


def run_compilation(
    input_path: str | Path,
    name: str,
    model: str = "sonnet",
    compilations_root: str | Path = COMPILATIONS_ROOT,
) -> str:
    """Synchronous convenience: prepare + run a compilation to completion, writing
    the full compilation object under <compilations_root>/<id>/. Returns the
    compilation_id. (The web app splits these two phases so it can redirect+poll;
    this single-call form is for the CLI / tests.)"""
    prep = prepare_compilation(input_path, name, model, compilations_root)
    return run_prepared_compilation(prep)


def list_compilations(
    compilations_root: str | Path = COMPILATIONS_ROOT,
) -> list[dict[str, Any]]:
    """Read every compilation manifest under compilations_root, newest first, for
    the index page (parallels runner._list_runs)."""
    compilations_root = Path(compilations_root)
    if not compilations_root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for comp in sorted(compilations_root.iterdir(), reverse=True):
        if not comp.is_dir():
            continue
        manifest_path = comp / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            m = {"compilation_id": comp.name, "status": "corrupt"}
        out.append({
            "compilation_id": m.get("compilation_id", comp.name),
            "created_at": m.get("created_at"),
            "name": m.get("name"),
            "input": m.get("input"),
            "model": m.get("model"),
            "status": m.get("status", "unknown"),
            "n_stages": m.get("n_stages", 0),
            "n_validation_issues": len(m.get("validation_issues") or []),
        })
    return out


def load_compilation(
    compilation_id: str,
    compilations_root: str | Path = COMPILATIONS_ROOT,
) -> dict[str, Any]:
    """Load a single compilation object (manifest + what_happened + DAG stages +
    methodology_raw.md) for the detail page. Raises FileNotFoundError if the
    manifest is missing. Tolerates a still-running / errored compile where the
    what_happened + DAG files do not yet exist."""
    comp_dir = Path(compilations_root) / compilation_id
    manifest_path = comp_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"No compilation '{compilation_id}'")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    what_happened: dict[str, Any] | None = None
    wh_path = comp_dir / "what_happened.json"
    if wh_path.exists():
        what_happened = json.loads(wh_path.read_text(encoding="utf-8"))

    stages: list[dict[str, Any]] = []
    compiled_dir = comp_dir / "dag" / "compiled"
    if compiled_dir.is_dir():
        # Read the draft stages as raw dicts (not typed Stages): the detail view
        # renders whatever compiled, including invalid drafts the strict loader
        # would reject.
        for stage_file in sorted(compiled_dir.glob("*.json")):
            data = json.loads(stage_file.read_text(encoding="utf-8")) or {}
            # build_mermaid_graph needs a fallback _filename; the run loader sets
            # these too. Keep id-bearing stages renderable.
            data["_filename"] = stage_file.name
            data["_order"] = stage_file.stem.split("_", 1)[0]
            stages.append(data)

    methodology_raw = ""
    raw_md_path = comp_dir / "dag" / "methodology_raw.md"
    if raw_md_path.exists():
        methodology_raw = raw_md_path.read_text(encoding="utf-8")

    error_text = None
    err_path = comp_dir / "error.txt"
    if err_path.exists():
        error_text = err_path.read_text(encoding="utf-8")

    return {
        "manifest": manifest,
        "what_happened": what_happened,
        "stages": stages,
        "methodology_raw": methodology_raw,
        "error_text": error_text,
    }

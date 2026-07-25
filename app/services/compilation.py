"""
compilation.py — the compile-writer service.

The compile MECHANISM (prose → draft workflow) lives in `app.compiler`. This service
owns persisting compile results to a project's disk layout: `write_methodology` writes
compiled stages to `<project_dir>/compiled/NN_<id>.json` + `methodology_raw.md`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_methodology(result: dict[str, Any], out_dir: str | Path) -> dict[str, Any]:
    """Write the compiled workflow to a folder shaped like a project artifact:
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

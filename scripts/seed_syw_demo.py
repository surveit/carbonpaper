"""Seed a small, clearly-synthetic demo run so the show-your-work tracer has a
row-preserving chain to walk in the browser. Writes to the gitignored
examples/_syw_demo/ tree. Synthetic scaffolding for looking at the UI — NOT real
analysis data.

Chain: load_mills (input_data) -> add_region (python_row_function)
       -> score (python_row_function). All 1:1 by position, so a trace from a
`score` row walks back through add_region to the load_mills origin.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from app.models import Stage
from app.services.loader import stage_to_spec_dict
from app.web.config import EXAMPLES_DIR

RUN_ID = "20260708T120000"

_ADD_REGION = "def transform(row):\n    row['region'] = REGION_BY_MILL[row['facility_id']]\n    return row"
_SCORE = "def transform(row):\n    row['risk_score'] = round(peat_pressure(row['region']), 2)\n    return row"

COMPILED = [
    ("01_load_mills", {
        "id": "load_mills", "type": "input_data", "name": "Load mill seeds (synthetic)",
        "connector": {"kind": "computed_static", "notes": "hand-seeded demo rows"},
    }),
    ("02_add_region", {
        "id": "add_region", "type": "python_row_function", "name": "Attach region (1:1)",
        "inputs": [{"id": "load_mills"}],
        "function": {"kind": "inline", "code": _ADD_REGION},
    }),
    ("03_score", {
        "id": "score", "type": "python_row_function", "name": "Compute risk score (1:1)",
        "inputs": [{"id": "add_region"}],
        "function": {"kind": "inline", "code": _SCORE},
    }),
]


def _write_compiled(project_dir: Path) -> None:
    compiled = project_dir / "compiled"
    compiled.mkdir(parents=True, exist_ok=True)
    for filename, spec in COMPILED:
        stage = Stage.model_validate(spec)  # validate the demo stays a real Stage
        (compiled / f"{filename}.json").write_text(
            json.dumps(stage_to_spec_dict(stage), indent=2), encoding="utf-8"
        )


def _write(run_dir: Path, stages: list[dict]) -> None:
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    records = []
    for spec in stages:
        rel = f"outputs/{spec['id']}.parquet"
        spec["df"].to_parquet(run_dir / rel, index=False)
        records.append({
            "stage_id": spec["id"],
            "type": spec["type"],
            "name": spec["name"],
            "status": "ok",
            "rows": len(spec["df"]),
            "output_path": rel,
            "input_validation": [
                {"stage_id": spec["id"], "phase": f"input:{p}", "ok": True, "issues": []}
                for p in spec.get("parents", [])
            ],
            "output_validation": {"rows": len(spec["df"]), "ok": True, "issues": []},
        })
    manifest = {
        "run_id": RUN_ID,
        "started_at": "2026-07-08T12:00:00",
        "finished_at": "2026-07-08T12:00:00",
        "methodology": "_syw_demo",
        "status": "ok",
        "stages": records,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def main() -> None:
    mills = pd.DataFrame({
        "facility_id": ["MILL-001", "MILL-002", "MILL-003"],
        "name": ["Example Mill A", "Example Mill B", "Example Mill C"],
    })
    with_region = mills.assign(region=["Riau", "Riau", "West Kalimantan"])
    scored = with_region.assign(risk_score=[0.12, 0.47, 0.83])

    project_dir = EXAMPLES_DIR / "_syw_demo"
    _write_compiled(project_dir)
    run_dir = project_dir / "runs" / RUN_ID
    _write(run_dir, [
        {"id": "load_mills", "type": "input_data", "parents": [],
         "name": "Load mill seeds (synthetic)", "df": mills},
        {"id": "add_region", "type": "python_row_function", "parents": ["load_mills"],
         "name": "Attach region (1:1)", "df": with_region},
        {"id": "score", "type": "python_row_function", "parents": ["add_region"],
         "name": "Compute risk score (1:1)", "df": scored},
    ])
    print(f"seeded {run_dir}")


if __name__ == "__main__":
    main()

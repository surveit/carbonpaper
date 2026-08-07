"""A run as a Jupyter notebook: one section per stage, loading the CSVs the packet
already carries. Sits beside them in the packet, so `data/…` resolves."""
from __future__ import annotations

import hashlib
import json
import keyword
from typing import Any

from app.services.loader import resolve_function_code
from app.services.review_packet.views import RunView, StageView

NOTEBOOK_FILE = "run.ipynb"

# The first thing the reader meets, and the claim the whole file rests on. A notebook
# invites "run this and see" — every cell here only re-reads a recorded output, so
# saying otherwise would let a reader mistake reading for reproducing.
CAVEAT = (
    "**This notebook does not reproduce the run.** It documents the stages and loads "
    "the intermediate data dumps for analysis. Every cell below reads a file this run "
    "already wrote, from the `data/` folder beside this notebook — nothing here "
    "recomputes anything, so running it cannot confirm or contradict the pipeline. To "
    "check where a figure came from, read the step and its inputs."
)

_FRAMES = "frames"


def build_run_notebook(view: RunView) -> str:
    cells = [_title_cell(view), _imports_cell()]
    for position, stage in enumerate(view.stages, start=1):
        cells.extend(_stage_cells(position, stage))
    return json.dumps(_wrap(cells), indent=1, sort_keys=True) + "\n"


def _stage_cells(position: int, stage: StageView) -> list[dict[str, Any]]:
    cells = [_markdown(_id(stage.stage_id, "head"), _heading(position, stage))]
    code = resolve_function_code(stage.definition)
    if code:
        cells.append(_markdown(_id(stage.stage_id, "note"), ["The code this step ran:"]))
        cells.append(_code(_id(stage.stage_id, "code"), code.splitlines()))
    if stage.data_file:
        cells.append(_code(_id(stage.stage_id, "load"), _load(stage)))
    return cells


def _heading(position: int, stage: StageView) -> list[str]:
    definition = stage.definition
    lines = [f"## {position}. `{stage.stage_id}`", "", f"**{stage.type}**"]
    if definition is not None and definition.description.strip():
        lines += ["", definition.description]
    elif stage.definition_error:
        lines += ["", f"_Definition unavailable: {stage.definition_error}_"]
    return lines + ["", f"`{stage.status}` · {stage.row_count:,} rows out "
                        f"· {stage.elapsed_ms} ms"]


def _load(stage: StageView) -> list[str]:
    # A snake_case id binds directly; one that is a Python keyword would not parse.
    read = f"pd.read_csv(f'{{DATA}}/{stage.stage_id}.csv')"
    if keyword.iskeyword(stage.stage_id):
        target = f"{_FRAMES}[{stage.stage_id!r}]"
    else:
        target = stage.stage_id
    return [f"{target} = {read}", f"print({target}.shape)", f"{target}.head()"]


def _title_cell(view: RunView) -> dict[str, Any]:
    return _markdown(_id("title"), [
        f"# {view.project} — run `{view.run_id}`",
        "",
        f"> {CAVEAT}",
        "",
        f"Status **{view.status}** · {len(view.stages)} steps"
        + (f" · workflow version `{view.workflow_version}`" if view.workflow_version else ""),
    ])


def _imports_cell() -> dict[str, Any]:
    return _code(_id("imports"), ["import pandas as pd", "", "DATA = 'data'",
                                  f"{_FRAMES} = {{}}"])


def _id(*parts: str) -> str:
    # Hashed from the stage id, never random, so two dumps of one run match.
    return hashlib.sha256("/".join(parts).encode()).hexdigest()[:16]


def _markdown(cell_id: str, lines: list[str]) -> dict[str, Any]:
    return {"cell_type": "markdown", "id": cell_id, "metadata": {},
            "source": [f"{line}\n" for line in lines]}


def _code(cell_id: str, lines: list[str]) -> dict[str, Any]:
    return {"cell_type": "code", "id": cell_id, "metadata": {}, "execution_count": None,
            "outputs": [], "source": [f"{line}\n" for line in lines]}


def _wrap(cells: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python",
                           "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }

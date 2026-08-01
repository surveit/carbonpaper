"""The packet's HTML half: an index and one page per stage, rendered from local
templates with relative links so the folder opens from disk with no server."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from pydantic import BaseModel

from app.models import Stage
from app.models.stages.llm_transform import LLMTransformStage
from app.services.loader import resolve_function_code, stage_to_spec_dict
from app.services.review_packet.checksums import CHECKSUMS_FILE
from app.services.review_packet.data import DataReport, read_stage_output
from app.services.review_packet.views import RunView, StageView

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
STYLESHEET = "packet.css"
ASSETS_DIR = "assets"
STAGES_DIR = "stages"

# Rows rendered into a stage page. The CSV beside it is uncapped, and the page
# says so — a silently truncated table would misstate how much data there is.
MAX_TABLE_ROWS = 200

# StrictUndefined: a template naming a field the view does not carry is a bug to
# surface, not an empty cell to render.
_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
    undefined=StrictUndefined,
)


class TableView(BaseModel):
    columns: list[str]
    rows: list[list[str]]
    rows_total: int
    capped: bool
    unreadable: str | None


class CornerCaseView(BaseModel):
    case: str
    expected: str


# Field order is the reading order the stage page follows: prose, then the prompt
# or code, then the raw spec — the reviewer is a journalist, not an engineer.
class TransformView(BaseModel):
    """What the stage did."""

    summary: str | None
    corner_cases: list[CornerCaseView]
    prompt_instructions: str | None
    prompt_data_template: str | None
    model: str | None
    code: str | None
    spec_json: str


def write_packet_pages(root: Path, view: RunView, data: DataReport) -> list[str]:
    """Writes index.html, a page per stage, and the stylesheet; returns their paths."""
    written = [_write_stylesheet(root)]
    written.append(_write_index(root, view, data))
    for stage in view.stages:
        written.append(_write_stage_page(root, view, stage))
    return written


def _write_index(root: Path, view: RunView, data: DataReport) -> str:
    html = _env.get_template("index.html").render(
        run=view,
        omitted=data.omitted,
        assets=f"{ASSETS_DIR}/{STYLESHEET}",
        stages_dir=STAGES_DIR,
        checksums_href=CHECKSUMS_FILE,
    )
    return _write_page(root / "index.html", html, "index.html")


def _write_stage_page(root: Path, view: RunView, stage: StageView) -> str:
    relative = f"{STAGES_DIR}/{stage.stage_id}.html"
    html = _env.get_template("stage.html").render(
        run=view,
        stage=stage,
        transform=_build_transform_view(stage.definition),
        table=_build_table_view(root, stage),
        assets=f"../{ASSETS_DIR}/{STYLESHEET}",
        index_href="../index.html",
        data_href=f"../{stage.data_file}" if stage.data_file else None,
        checksums_href=f"../{CHECKSUMS_FILE}",
    )
    return _write_page(root / relative, html, relative)


def _write_stylesheet(root: Path) -> str:
    relative = f"{ASSETS_DIR}/{STYLESHEET}"
    dest = root / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text((STATIC_DIR / STYLESHEET).read_text(encoding="utf-8"), encoding="utf-8")
    return relative


def _write_page(dest: Path, html: str, relative: str) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(html, encoding="utf-8")
    return relative


def _build_transform_view(stage: Stage | None) -> TransformView | None:
    if stage is None:
        return None
    code_block = stage.find_authored_code_block()
    llm = stage.llm if isinstance(stage, LLMTransformStage) else None
    return TransformView(
        summary=getattr(code_block, "summary", None),
        corner_cases=_read_corner_cases(code_block),
        prompt_instructions=llm.prompt_instructions if llm else None,
        prompt_data_template=llm.prompt_data_template if llm else None,
        model=str(llm.model) if llm and llm.model else None,
        code=resolve_function_code(stage),
        spec_json=_dump_spec(stage),
    )


def _read_corner_cases(code_block: Any) -> list[CornerCaseView]:
    cases = getattr(code_block, "corner_cases", None) or []
    return [CornerCaseView(case=c.case, expected=c.expected) for c in cases]


def _dump_spec(stage: Stage) -> str:
    return json.dumps(stage_to_spec_dict(stage), indent=2, sort_keys=True)


def _build_table_view(root: Path, stage: StageView) -> TableView | None:
    """Reads back the packet's own CSV, so page and download cannot disagree."""
    if stage.data_file is None:
        return None
    path = root / stage.data_file
    if not path.is_file():
        return None
    try:
        frame = read_stage_output(path)
    except (pd.errors.ParserError, UnicodeDecodeError, ValueError) as exc:
        return TableView(
            columns=[], rows=[], rows_total=0, capped=False, unreadable=str(exc)
        )
    return _shape_table(frame)


def _shape_table(frame: pd.DataFrame) -> TableView:
    head = frame.head(MAX_TABLE_ROWS)
    return TableView(
        columns=[str(c) for c in frame.columns],
        rows=[[_render_cell(v) for v in row] for row in head.itertuples(index=False)],
        rows_total=len(frame),
        capped=len(frame) > len(head),
        unreadable=None,
    )


def _render_cell(value: Any) -> str:
    """Empty string for a null: a rendered "nan" reads as real data."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value)


__all__ = [
    "ASSETS_DIR",
    "MAX_TABLE_ROWS",
    "STAGES_DIR",
    "STYLESHEET",
    "TableView",
    "TransformView",
    "write_packet_pages",
]

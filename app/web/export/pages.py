"""The packet's HTML: the app's own run templates, rendered to files with
relative links. Lives under app/web because that is where those templates are."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.stage_display import TYPE_CLASS, TYPE_GLYPH
from app.services.loader import resolve_function_code
from app.services.review_packet.checksums import CHECKSUMS_FILE
from app.services.review_packet.data import DataReport
from app.services.review_packet.views import RunView, StageView
from app.services.run_guide import RunGuideView
from app.web.config import templates
from app.web.loading import load_output_preview, load_output_table
from app.web.panel_links import PacketPanelLinks

ASSETS_DIR = "assets"
STAGES_DIR = "stages"
APP_STYLESHEET = "style.css"
PACKET_STYLESHEET = "packet.css"
STYLESHEETS = (APP_STYLESHEET, PACKET_STYLESHEET)

_APP_STATIC = Path(__file__).resolve().parents[2] / "static"
_PACKET_STATIC = Path(__file__).parent / "static"


def write_packet_pages(
    root: Path,
    run_dir: Path,
    view: RunView,
    data: DataReport,
    guide: RunGuideView | None,
) -> list[str]:
    """index.html, one page per stage, and the stylesheets; returns their paths."""
    written = _write_stylesheets(root)
    written.append(_write_index(root, view, data, guide))
    for stage in view.stages:
        written.append(_write_stage_page(root, run_dir, view, stage))
    return written


def _write_index(
    root: Path, view: RunView, data: DataReport, guide: RunGuideView | None
) -> str:
    html = _render(
        "packet_index.html",
        run=view,
        guide=guide,
        omitted=data.omitted,
        assets=[f"{ASSETS_DIR}/{name}" for name in STYLESHEETS],
        stages_dir=STAGES_DIR,
        checksums_href=CHECKSUMS_FILE,
        project=view.project,
        links=PacketPanelLinks(),
        type_glyph=TYPE_GLYPH,
        type_class=TYPE_CLASS,
    )
    return _write(root / "index.html", html, "index.html")


def _write_stage_page(root: Path, run_dir: Path, view: RunView, stage: StageView) -> str:
    """Wraps the app's real `_run_stage_panel.html`, the panel the author reviewed in."""
    relative = f"{STAGES_DIR}/{stage.stage_id}.html"
    html = _render(
        "packet_stage.html",
        run=view,
        assets=[f"../{ASSETS_DIR}/{name}" for name in STYLESHEETS],
        index_href="../index.html",
        checksums_href=f"../{CHECKSUMS_FILE}",
        **_build_panel_context(run_dir, view, stage),
    )
    return _write(root / relative, html, relative)


def _build_panel_context(run_dir: Path, view: RunView, stage: StageView) -> dict[str, Any]:
    """`previewable` False and `test_views` empty are what make the panel inert."""
    # They drop the scratch-run controls and the examples section. Examples are
    # omitted because running them now would report today's code, not this run's.
    return {
        "project": view.project,
        "run_id": view.run_id,
        "stage": stage.record,
        "stage_def": stage.definition,
        "stage_def_error": stage.definition_error,
        "preview": _load_full_table(run_dir, stage),
        "input_previews": _build_input_previews(run_dir, view, stage),
        "function_code": resolve_function_code(stage.definition),
        "llm_example": None,
        "test_views": [],
        "test_derivable": False,
        "certification": None,
        "previewable": False,
        "links": PacketPanelLinks(),
        "type_glyph": TYPE_GLYPH,
        "type_class": TYPE_CLASS,
    }


def _load_full_table(run_dir: Path, stage: StageView) -> dict[str, Any] | None:
    """The whole table to load_output_table's 5000-row cap, not the 5-row preview."""
    # The packet is read offline, with no route to click through to. The panel
    # template states the cap itself when it bites.
    if stage.output_path is None or not (run_dir / stage.output_path).is_file():
        return None
    table = load_output_table(run_dir, stage.output_path)
    return {
        "columns": table["columns"],
        "preview": table["rows"],
        "rows_total": table["rows_total"],
        "capped": table["capped"],
    }


def _build_input_previews(
    run_dir: Path, view: RunView, stage: StageView
) -> list[dict[str, Any]]:
    if stage.definition is None:
        return []
    outputs = {s.stage_id: s.output_path for s in view.stages}
    return [
        {"id": input_id, "preview": load_output_preview(run_dir, outputs.get(input_id))}
        for input_id in stage.definition.input_ids
    ]


def _render(template: str, **context: Any) -> str:
    """Through the app's own Jinja environment, so filters and templates match."""
    return templates.env.get_template(template).render(**context)


def _write(dest: Path, html: str, relative: str) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(html, encoding="utf-8")
    return relative


def _write_stylesheets(root: Path) -> list[str]:
    sources = {APP_STYLESHEET: _APP_STATIC, PACKET_STYLESHEET: _PACKET_STATIC}
    return [_copy_stylesheet(root, name, sources[name]) for name in STYLESHEETS]


def _copy_stylesheet(root: Path, name: str, source_dir: Path) -> str:
    relative = f"{ASSETS_DIR}/{name}"
    dest = root / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text((source_dir / name).read_text(encoding="utf-8"), encoding="utf-8")
    return relative


__all__ = ["ASSETS_DIR", "STAGES_DIR", "STYLESHEETS", "write_packet_pages"]

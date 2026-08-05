"""The packet's HTML: the app's own run templates, rendered to files with
relative links. Lives under app/web because that is where those templates are."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.stage_display import TYPE_CLASS, TYPE_GLYPH
from app.runtime.manifest import resolve_output_path
from app.services.loader import resolve_function_code
from app.services.review_packet.checksums import CHECKSUMS_FILE
from app.services.review_packet.data import DataReport
from app.services.review_packet.views import RunView, StageView
from app.services.run_guide import RunGuideView
from app.web.config import templates
from app.web.loading import load_output_preview, load_output_table
from app.web.panel_links import PacketPanelLinks

# The packet has no route to a further page, so a stage's table has to carry the
# rows outright. Still capped: a browser opening a static file has no pagination
# to fall back on. Past this the page says so and the reader goes to data/*.csv,
# which is written uncapped.
PACKET_MAX_TABLE_ROWS = 50_000

ASSETS_DIR = "assets"
STAGES_DIR = "stages"
APP_STYLESHEET = "style.css"
PACKET_STYLESHEET = "packet.css"
STYLESHEETS = (APP_STYLESHEET, PACKET_STYLESHEET)

_APP_STATIC = Path(__file__).resolve().parents[2] / "static"
_PACKET_STATIC = Path(__file__).parent / "static"

# The node-click dispatcher, vendored so the packet's graph nodes are live.
NODE_SCRIPT = "diagram_nodes.js"

# The diagram renderer is the packet's ONE external request; the index says so.
# Version-pinned rather than `mermaid@11`, so the URL and the hash cannot drift
# apart: a floating tag would start failing SRI the day jsDelivr serves 11.17.
MERMAID_URL = "https://cdn.jsdelivr.net/npm/mermaid@11.16.0/dist/mermaid.min.js"
MERMAID_SRI = "sha384-T/0lMUdJpd2S1ZHtRiofG3htU3xPCrFVeAQ1UUE2TJwlEJSV5NUwn30kP28n238E"
WORKFLOW_DIAGRAM_FILE = "workflow.mmd"


def write_packet_pages(
    root: Path,
    run_dir: Path,
    view: RunView,
    data: DataReport,
    guide: RunGuideView | None,
    diagram: str,
) -> list[str]:
    """index.html, one page per stage, the stylesheets and the diagram source."""
    written = _write_stylesheets(root)
    written.append(_write_node_script(root))
    written.append(_write_diagram_source(root, diagram))
    written.append(_write_index(root, view, data, guide, diagram))
    for stage in view.stages:
        written.append(_write_stage_page(root, run_dir, view, stage))
    return written


def _write_index(
    root: Path,
    view: RunView,
    data: DataReport,
    guide: RunGuideView | None,
    diagram: str,
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
        mermaid=diagram,
        mermaid_url=MERMAID_URL,
        mermaid_sri=MERMAID_SRI,
        diagram_source_href=WORKFLOW_DIAGRAM_FILE,
        node_script=f"{ASSETS_DIR}/{NODE_SCRIPT}",
        type_glyph=TYPE_GLYPH,
        type_class=TYPE_CLASS,
    )
    return _write(root / "index.html", html, "index.html")


def _write_diagram_source(root: Path, diagram: str) -> str:
    """The flowchart as text, so the diagram outlives the CDN link rotting."""
    return _write_text(root / WORKFLOW_DIAGRAM_FILE, diagram, WORKFLOW_DIAGRAM_FILE)


def _write_node_script(root: Path) -> str:
    relative = f"{ASSETS_DIR}/{NODE_SCRIPT}"
    return _write_text(
        root / relative, (_APP_STATIC / NODE_SCRIPT).read_text(encoding="utf-8"), relative
    )


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
    # The False/empty entries below are what make the packet's panel inert.
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
        "can_generate_tests": False,
        "certification": None,
        "previewable": False,
        "links": PacketPanelLinks(),
        "type_glyph": TYPE_GLYPH,
        "type_class": TYPE_CLASS,
    }


def _load_full_table(run_dir: Path, stage: StageView) -> dict[str, Any] | None:
    # Rendered once to a file, not per request, so it carries far more than a page would.
    source = resolve_output_path(run_dir, stage.output_path)
    if source is None or not source.is_file():
        return None
    table = load_output_table(run_dir, stage.output_path, PACKET_MAX_TABLE_ROWS)
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
    return _write_text(dest, html, relative)


def _write_text(dest: Path, text: str, relative: str) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
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

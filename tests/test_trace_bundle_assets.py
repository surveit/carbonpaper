"""What a copied bundle must be able to do on its own: style the trace body it
ships, and carry nothing it never uses."""
from __future__ import annotations

import re
from pathlib import Path

from app.runtime.trace_page import TEMPLATES_DIR
from test_export_row_trace import SCORE_ID, at, exporter  # noqa: F401  (fixture)

_CLASS_ATTR = re.compile(r'class="([^"{}]+)"')
_STYLESHEET_HREF = re.compile(r'<link[^>]+rel="stylesheet"[^>]+href="([^"]+)"')


def _classes_the_body_emits() -> set[str]:
    source = (TEMPLATES_DIR / "_trace_body.html").read_text(encoding="utf-8")
    return {name for attr in _CLASS_ATTR.findall(source) for name in attr.split()}


def _rules_the_page_loads(page: Path) -> str:
    hrefs = _STYLESHEET_HREF.findall(page.read_text(encoding="utf-8"))
    assert hrefs, f"{page.name} loads no stylesheet at all"
    sheets = [(page.parent / href).resolve() for href in hrefs]
    missing = [s for s in sheets if not s.is_file()]
    assert not missing, f"unresolvable stylesheets: {missing}"
    return "\n".join(s.read_text(encoding="utf-8") for s in sheets)


def _export(exporter) -> Path:  # noqa: F811
    from_file = exporter.output_dir / "index.html"
    href = exporter.export_row_trace(SCORE_ID, from_file, row=at(0))
    return (from_file.parent / href).resolve()


def test_the_bundled_stylesheets_carry_a_rule_for_every_class_the_body_emits(exporter):  # noqa: F811
    rules = _rules_the_page_loads(_export(exporter))
    unstyled = [c for c in sorted(_classes_the_body_emits())
                if not re.search(rf"\.{re.escape(c)}\b", rules)]
    assert not unstyled, f"trace body classes with no rule in the bundle: {unstyled}"


def test_the_live_lineage_page_loads_the_same_trace_stylesheet(exporter):  # noqa: F811
    _export(exporter)
    exported = {s.name for s in
                (exporter.output_dir / "_assets").iterdir() if s.suffix == ".css"}
    live = Path(__file__).resolve().parents[1] / "app/templates/lineage.html"
    linked = set(_STYLESHEET_HREF.findall(live.read_text(encoding="utf-8")))
    for name in exported:
        assert f"/static/{name}" in linked, (
            f"the bundle ships {name} but the live lineage page does not load it — "
            f"the two would drift")


def test_the_bundle_ships_no_mermaid_library(exporter):  # noqa: F811
    page = _export(exporter)
    shipped = [p.name for p in exporter.output_dir.rglob("*")
               if p.is_file() and "mermaid" in p.name.lower()]
    assert not shipped, f"bundle still ships mermaid: {shipped}"
    assert "mermaid" not in page.read_text(encoding="utf-8").lower()


def test_the_repo_no_longer_vendors_mermaid():
    static = Path(__file__).resolve().parents[1] / "app/static"
    assert not list(static.rglob("mermaid*")), "vendored mermaid is still in the repo"

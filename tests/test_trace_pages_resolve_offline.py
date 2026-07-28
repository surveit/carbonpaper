"""Proves the wiring: a generated trace page's own asset references resolve to
files on disk. Stronger than checking they aren't absolute — it also catches
an asset_prefix computed at the wrong depth, which a relative-but-wrong prefix
would pass unnoticed."""
import re

from test_export_row_trace import SCORE_ID, exporter  # noqa: F401  (fixture)

REF = re.compile(r"""\b(?:src|href)\s*=\s*["'](?P<url>[^"']+)["']""")


def test_every_asset_a_generated_trace_page_references_exists_on_disk(exporter):  # noqa: F811
    from_file = exporter.output_dir / "index.html"
    href = exporter.export_row_trace(SCORE_ID, 0, from_file)
    page = (from_file.parent / href).resolve()

    refs = [m.group("url") for m in REF.finditer(page.read_text(encoding="utf-8"))]
    assert refs, "the trace page referenced no assets at all"

    missing = [r for r in refs if not (page.parent / r).resolve().is_file()]
    assert not missing, f"unresolvable references in {page.name}: {missing}"

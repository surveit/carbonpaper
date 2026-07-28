"""A published dossier legitimately contains https:// source citations — this
guards only the *renderer's own* asset references (src=/href=), which must stay
relative so a copied bundle resolves with no network."""
import re

from app.runtime.trace_page import TEMPLATES_DIR

# Any src=/href= whose value begins with a scheme or protocol-relative "//".
ABSOLUTE_ASSET_REF = re.compile(
    r"""\b(?:src|href)\s*=\s*["'](?P<url>(?:[a-zA-Z][a-zA-Z0-9+.-]*:)?//[^"']*)["']"""
)


def test_runtime_trace_templates_reference_only_local_assets():
    offenders = {}
    for template in TEMPLATES_DIR.rglob("*.html"):
        found = ABSOLUTE_ASSET_REF.findall(template.read_text(encoding="utf-8"))
        if found:
            offenders[template.name] = found
    assert not offenders, (
        f"trace templates must reference assets relatively so a copied bundle "
        f"resolves with no network; found: {offenders}"
    )


def test_the_pattern_catches_any_scheme_not_a_blocklisted_host():
    for url in ("https://cdn.jsdelivr.net/x.js", "http://a/b.css", "//unpkg.com/x"):
        assert ABSOLUTE_ASSET_REF.search(f'<script src="{url}"></script>'), url
    assert not ABSOLUTE_ASSET_REF.search('<script src="../_assets/mermaid.min.js">')

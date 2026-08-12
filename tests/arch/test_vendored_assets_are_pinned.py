"""Architecture: a vendored asset is byte-identical to the release it claims to be.

It is the whole price of the exemptions in test_palette_owns_colour.py and
test_transform_level_stays_out_of_the_ui.py: both skip these files on the grounds
that upstream wrote them, and this is what keeps that true.
"""
from __future__ import annotations

from arch.vendored import HIGHLIGHT_JS_VERSION, STATIC, VENDORED_SRI, read_sri


def test_vendored_assets_match_their_upstream_release() -> None:
    drifted = {
        name: read_sri(STATIC / name)
        for name, expected in VENDORED_SRI.items()
        if read_sri(STATIC / name) != expected
    }
    assert not drifted, (
        f"vendored asset does not match highlight.js {HIGHLIGHT_JS_VERSION} as cdnjs "
        f"publishes it: {drifted}. Re-download it, or bump the version and its hash "
        "together — hand-editing a vendored file is what this rule exists to stop, "
        "because two other tests excuse these files on the strength of it."
    )

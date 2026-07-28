"""Architecture: node_review.py stays a dependency leaf.

Default-deny over first-party imports, so app.runtime and app.compiler are
denied without being named. app.core.utils (compute_short_hash) is the single
permitted first-party import.
"""
from __future__ import annotations

from arch import find_governed_files
from arch.import_allowlist import find_disallowed_imports

_LEAF_ALLOW = {"app.core.utils"}


def test_node_review_imports_nothing_but_the_hash_primitive() -> None:
    node_review = [p for p in find_governed_files(__file__) if p.name == "node_review.py"]
    assert node_review, "expected app/services/node_review.py in this arch test's scope"
    offenders = find_disallowed_imports(node_review, roots={"app"}, allow=_LEAF_ALLOW)
    assert not offenders, (
        "app/services/node_review.py must stay a leaf both the routes layer and "
        "the versioning layer can lean on: stdlib + pandas, plus app.core.utils "
        "for the content hash — nothing from app.runtime, app.compiler, or any "
        "other app package. Move the logic that needs the new dependency into "
        "its caller instead of importing it here. Offending imports:\n  "
        + "\n  ".join(offenders)
    )

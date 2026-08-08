"""Architecture: a JSON document is read off disk only via read_json_document.

Every governed module parses a record file through the chokepoint; none inlines
`json.loads(path.read_text())` / `json.load(open(path))` on its own. Scope is all of
``app/``; streams, bulk uploads, artifacts, and fixtures are out of scope by design.
"""
from __future__ import annotations

from arch import find_governed_files, find_inline_json_disk_reads


def test_json_documents_are_read_only_by_the_chokepoint() -> None:
    offenders = find_inline_json_disk_reads(
        find_governed_files(__file__),
        allow={
            # The chokepoint itself: this is the one sanctioned inline read.
            "app/core/json_document.py",
            # Bulk data: a user-uploaded geojson input file, not a record.
            "app/runtime/stages/input_data.py",
        },
    )
    assert not offenders, (
        "a JSON document must be read only via "
        "app.core.json_document.read_json_document, never parsed inline off disk:\n  "
        + "\n  ".join(offenders)
    )

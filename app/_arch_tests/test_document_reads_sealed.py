"""Architecture: a JSON document is read off disk only via read_json_document.

Recognizes two literal call forms under ``app/`` — `json.loads(<path>.read_text())`
(or `read_bytes`) and `json.load(open(<path>))` — and nothing else. A tripwire that
stops the routed readers re-scattering, NOT proof that no other read exists.
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
    # `allow` is the exemption set, and it is SHORTER than the set of reads this file
    # leaves alone. Three the detector provably cannot see, so they are absent from
    # `allow` without anyone having exempted them: a read staged through a variable
    # (`text = p.read_text()` on one line, `json.loads(text)` on the next); a model
    # parsing the text itself, `WorkflowFile.model_validate_json(path.read_text(...))`,
    # live today in app/seeds/seed.py and app/web/routers/admin.py; and an aliased
    # `import json as j`. Every other read left alone — append-and-tail run logs,
    # bulk uploads, published artifacts, committed fixtures — is likewise outside the
    # DETECTOR's reach, not exempted here, so `allow` must not be read as naming them.
    assert not offenders, (
        "a JSON document must be read only via "
        "app.core.json_document.read_json_document, never via "
        "json.loads(path.read_text()) or json.load(open(path)):\n  "
        + "\n  ".join(offenders)
    )

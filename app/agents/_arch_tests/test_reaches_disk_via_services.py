"""Architecture: the methodology-editing agent reaches state through services, not disk.

``app/agents`` is the domain agent — its tools edit a project's methodology.
Persistence is owned by ``app.services`` (and the loader beneath it), so these tools
must never open files directly: one disk owner, and the tools stay testable without a
filesystem. Scope is this feature's own subtree, derived from where this test lives.
"""
from __future__ import annotations

from arch import check_no_raw_disk, find_governed_files


def test_reaches_disk_only_through_services() -> None:
    offenders = check_no_raw_disk(find_governed_files(__file__))
    assert not offenders, (
        "app/agents must persist via app.services, not raw file I/O:\n  "
        + "\n  ".join(offenders)
    )

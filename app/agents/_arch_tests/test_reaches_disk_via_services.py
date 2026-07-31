"""Architecture: the methodology-editing agent reaches state through services, not disk.

Scope is this feature's own subtree, taken from where this test lives.
"""
from __future__ import annotations

from arch import check_no_raw_disk, find_governed_files


def test_reaches_disk_only_through_services() -> None:
    offenders = check_no_raw_disk(find_governed_files(__file__))
    assert not offenders, (
        "app/agents must persist via app.services, not raw file I/O:\n  "
        + "\n  ".join(offenders)
    )

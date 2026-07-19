"""Architecture: no bespoke document I/O remains outside the document store.

Two invariants earlier persistence conversions already achieved: the eval
conversion retired all YAML file I/O (eval configs persist as document-store
JSON now), and the version conversion retired the directory-copy snapshot
mechanism (a version is an embedded document now, not a copied `versions/<id>/`
tree). Scope is all of `app/` (this test sits at its root); `examples/` and
scratch dirs are out of scope by design.

This is the first check in the "no bespoke doc I/O remains" ratchet — a
broader allowlist test over every disk read/write follows in a later slice.
"""
from __future__ import annotations

from arch import check_no_call, check_no_import, find_governed_files


def test_no_yaml_anywhere() -> None:
    offenders = check_no_import(find_governed_files(__file__), "yaml", allow=set())
    assert not offenders, (
        "no module in app/ may import yaml (retired by the eval conversion — "
        "eval configs persist as document-store JSON now):\n  "
        + "\n  ".join(offenders)
    )


def test_no_shutil_copytree_anywhere() -> None:
    offenders = check_no_call(find_governed_files(__file__), {"copytree"})
    assert not offenders, (
        "no module in app/ may call shutil.copytree (retired by the version "
        "conversion — version snapshots are embedded documents now):\n  "
        + "\n  ".join(offenders)
    )

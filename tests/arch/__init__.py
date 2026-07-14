"""The ``arch`` architecture-testing toolkit.

Location-scoped file discovery (``find_governed_files`` / ``scan_all_source``) and
reusable structural predicates (``check_*``) for the tests in ``_arch_tests/`` folders
and the global rules in this package. Import-graph boundaries live separately in
pyproject ``[tool.importlinter]``; this toolkit holds the content-level AST checks.
"""
from __future__ import annotations

from arch.predicates import check_no_fabricated_numbers, check_no_raw_disk
from arch.scope import find_governed_files, scan_all_source

__all__ = [
    "find_governed_files",
    "scan_all_source",
    "check_no_raw_disk",
    "check_no_fabricated_numbers",
]

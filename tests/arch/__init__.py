"""The ``arch`` architecture-testing toolkit.

Location-scoped file discovery (``find_governed_files`` / ``scan_all_source``) and
reusable structural predicates (``check_*``). A rule scopes itself either to a
subtree (``find_governed_files(__file__)``) or to the whole repo
(``scan_all_source()``). Import-graph boundaries live separately in pyproject
``[tool.importlinter]``; this toolkit holds the content-level AST checks.
"""
from __future__ import annotations

from arch.predicates import (
    check_imports_are_stdlib_only,
    check_no_dict_keys,
    check_no_fabricated_numbers,
    check_no_import,
    check_no_raw_disk,
    find_check_prefixed_functions,
    find_production_run_imports,
)
from arch.scope import find_governed_files, scan_all_source

__all__ = [
    "find_governed_files",
    "scan_all_source",
    "check_no_raw_disk",
    "check_no_fabricated_numbers",
    "check_no_import",
    "check_imports_are_stdlib_only",
    "check_no_dict_keys",
    "find_check_prefixed_functions",
    "find_production_run_imports",
]

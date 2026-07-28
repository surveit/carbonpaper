"""Location-scoped file discovery plus reusable structural predicates for the
architecture tests. Import-graph boundaries live separately in pyproject
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
    find_private_name_imports,
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
    "find_private_name_imports",
    "find_production_run_imports",
]

"""Location-scoped file discovery plus reusable structural predicates for the
architecture tests. Import-graph boundaries live separately in pyproject
``[tool.importlinter]``; this toolkit holds the content-level AST checks.
"""
from __future__ import annotations

from arch.predicates import (
    check_imports_are_stdlib_only as check_imports_are_stdlib_only,
    check_no_dict_keys as check_no_dict_keys,
    check_no_fabricated_numbers as check_no_fabricated_numbers,
    check_no_import as check_no_import,
    check_no_raw_disk as check_no_raw_disk,
    find_banned_words as find_banned_words,
    find_check_prefixed_functions as find_check_prefixed_functions,
    find_inline_json_disk_reads as find_inline_json_disk_reads,
    find_production_run_imports as find_production_run_imports,
)
from arch.scope import (
    find_governed_files as find_governed_files,
    scan_all_source as scan_all_source,
    scan_all_text as scan_all_text,
)

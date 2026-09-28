"""app/_arch_tests/test_validate_not_check.py's rule, scoped to critic/ by where this file lives."""
from __future__ import annotations

from arch import find_check_prefixed_functions, find_governed_files


def test_no_check_prefixed_function_names() -> None:
    offenders = find_check_prefixed_functions(find_governed_files(__file__))
    assert not offenders, (
        "function names must not lead with check_/_check_ — name them "
        "validate_* (or find_* when they return the offenders):\n  "
        + "\n  ".join(offenders)
    )

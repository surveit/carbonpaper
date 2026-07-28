"""Architecture: the verb for an invariant is ``validate``, never ``check``.

A function that raises on (or reports) a broken invariant is ``validate_*``; one that
returns the offending items is ``find_*``. ``check_*`` / ``_check_*`` names are banned.
Scope is all of ``app/``, derived from where this test lives.
"""
from __future__ import annotations

from arch import find_check_prefixed_functions, find_governed_files


def test_no_check_prefixed_function_names() -> None:
    offenders = find_check_prefixed_functions(find_governed_files(__file__))
    assert not offenders, (
        "function names must not lead with check_/_check_ — name them "
        "validate_* (or find_* when they return the offenders):\n  "
        + "\n  ".join(offenders)
    )

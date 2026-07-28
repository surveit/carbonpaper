"""Architecture: no silent numeric fallback via ``dict.get`` (the cardinal rule).

Scope is the whole repo (minus tests and non-source dirs): a fabricated number is as
forbidden in a script as in ``app/``.
"""
from __future__ import annotations

from arch import check_no_fabricated_numbers, scan_all_source


def test_no_silent_numeric_get_fallback() -> None:
    offenders = check_no_fabricated_numbers(scan_all_source())
    assert not offenders, (
        "silent numeric .get() fallback - fail loud, return None, or justify with "
        "`# data-default-ok: <reason>`:\n  " + "\n  ".join(offenders)
    )

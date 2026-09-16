"""Architecture: no module under app/ imports the eval harness.

evals/ is a top-level tool that runs an eval against the app; the arrow never
runs back. Enforced as an allowlist (empty), not a forbidden import-linter
contract, which tests/arch/test_contracts_are_whitelists.py rejects.
"""
from __future__ import annotations

from arch import check_no_import, find_governed_files


def test_no_app_module_imports_the_eval_harness() -> None:
    offenders = check_no_import(find_governed_files(__file__), "evals", allow=set())
    assert not offenders, (
        "app/ must not import evals: the harness runs the product, not the other way "
        "round, and evals/harness/ may import only the standard library, pydantic and "
        "itself (tests/arch/test_eval_harness_imports_only_stdlib_and_pydantic.py). An "
        "app module reaching for it would make the product depend on its own test "
        "harness. Offending files:\n  " + "\n  ".join(offenders)
    )

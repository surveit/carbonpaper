"""Architecture: `app/runtime/stages/starlark_marshal.py` — a stage handler —
must not import numpy or pandas. Cell-type classification is framestore
knowledge (`app.core.frames`); pandas concepts must not come up this far.
"""
from __future__ import annotations

from pathlib import Path

from arch._helpers import find_imported_modules, parse_module

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TARGET = _REPO_ROOT / "app" / "runtime" / "stages" / "starlark_marshal.py"
_BANNED_PREFIXES = ("numpy", "pandas")


def find_banned_data_library_imports(modules: set[str]) -> list[str]:
    """Every module name in `modules` that is `numpy`/`pandas` or one of their
    submodules."""
    return sorted(
        name
        for name in modules
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in _BANNED_PREFIXES)
    )


def test_starlark_marshal_imports_neither_numpy_nor_pandas() -> None:
    offenders = find_banned_data_library_imports(find_imported_modules(parse_module(_TARGET)))
    assert not offenders, (
        f"{_TARGET}: cell-type classification belongs in app/core/frames.py, one "
        f"layer down — found: {offenders}"
    )


def test_find_banned_data_library_imports_flags_numpy_and_pandas_submodules() -> None:
    assert find_banned_data_library_imports({"numpy", "pandas.api.types", "app.core.frames"}) == [
        "numpy",
        "pandas.api.types",
    ]


def test_find_banned_data_library_imports_ignores_a_clean_module_set() -> None:
    assert find_banned_data_library_imports({"datetime", "app.core.frames"}) == []

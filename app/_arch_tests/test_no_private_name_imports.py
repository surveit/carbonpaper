"""Architecture: no module under ``app/`` reaches for another module's
``_``-prefixed name. Scope is all of ``app/``, derived from where this test
lives, with no allowlist. ``tests/`` is outside the rule — test modules reach
into the private helpers of the code they cover in dozens of places, and
narrowing that is a separate burn-down."""
from __future__ import annotations

from pathlib import Path

from arch import find_governed_files, find_private_name_imports


def test_app_modules_never_import_a_private_name() -> None:
    offenders = find_private_name_imports(find_governed_files(__file__))
    assert not offenders, (
        "a `_`-prefixed name belongs to its own module — a second module that "
        "imports one pins itself to a private detail and drifts when the owner "
        "changes it. Promote the name to the owner's public surface (drop the "
        "underscore) or move the shared logic to a module both may import:\n  "
        + "\n  ".join(offenders)
    )


# --- unit tests for find_private_name_imports, on inline snippets ---


def test_find_private_name_imports_flags_an_absolute_from_import(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("from app.runtime.execution import _strip_markers\n")
    assert len(find_private_name_imports([target])) == 1


def test_find_private_name_imports_flags_a_relative_aliased_import(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("from .execution import _strip_markers as strip\n")
    assert len(find_private_name_imports([target])) == 1


def test_find_private_name_imports_flags_a_module_attribute(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("import app.runtime.execution as ex\n\nex._strip_markers(frame)\n")
    assert len(find_private_name_imports([target])) == 1


def test_find_private_name_imports_ignores_public_names_and_dunders(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text(
        "import app.runtime.execution as ex\n"
        "from app._private_pkg.mod import strip_markers\n"
        "\n"
        "print(ex.__name__)\n"
        "self._own_helper()\n"
        "frame._is_copy\n"
    )
    assert find_private_name_imports([target]) == []

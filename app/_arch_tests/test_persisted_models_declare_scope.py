"""Architecture: every `PersistedModel` subclass declares its permission scope.

`app.core.persistence.PersistenceScope` is a permission profile for run
activity (see its docstring): `RUN`, `AUTHORED`, or `CROSS_RUN`. The base
class carries no default for `SCOPE` — nothing at runtime reads `SCOPE`, so
an omitted declaration is a modeling gap only this arch test catches, at
review time; there is no runtime check. A second test enforces the sharper
rule for the one scope that grants a run a write outliving it: a class
carrying `SCOPE = PersistenceScope.CROSS_RUN` must also define `for_mode`,
the view that revokes that write for a non-production run.

Scope is all of `app/` (this test sits at its root); detection is AST-only —
neither test imports the modules it inspects.
"""
from __future__ import annotations

import ast
from pathlib import Path

from arch import find_governed_files
from arch._helpers import (
    find_class_body_assignment,
    find_class_body_function,
    find_subclasses_of,
    parse_module,
)

_PERSISTED_MODEL = "PersistedModel"
_SCOPE_ATTR = "SCOPE"
_CROSS_RUN_ATTR = "CROSS_RUN"
_FOR_MODE_METHOD = "for_mode"


def find_undeclared_scope_offenders(paths: list[Path]) -> list[str]:
    """"<path>:<lineno>  class <name>" for every PersistedModel subclass under
    `paths` whose class body never assigns `SCOPE` a value."""
    offenders: list[str] = []
    for path in paths:
        tree = parse_module(path)
        for node in find_subclasses_of(tree, _PERSISTED_MODEL):
            if find_class_body_assignment(node, _SCOPE_ATTR) is None:
                offenders.append(f"{path.name}:{node.lineno}  class {node.name}")
    return offenders


def find_cross_run_missing_for_mode_offenders(paths: list[Path]) -> list[str]:
    """"<path>:<lineno>  class <name>" for every PersistedModel subclass whose
    `SCOPE` is assigned `PersistenceScope.CROSS_RUN` but whose class body
    never defines `for_mode`."""
    offenders: list[str] = []
    for path in paths:
        tree = parse_module(path)
        for node in find_subclasses_of(tree, _PERSISTED_MODEL):
            scope_stmt = find_class_body_assignment(node, _SCOPE_ATTR)
            if scope_stmt is None or not _assigns_cross_run(scope_stmt):
                continue
            if find_class_body_function(node, _FOR_MODE_METHOD) is None:
                offenders.append(f"{path.name}:{node.lineno}  class {node.name}")
    return offenders


def _assigns_cross_run(stmt: ast.Assign | ast.AnnAssign) -> bool:
    """True if a `find_class_body_assignment` hit's value is an attribute
    access ending in `CROSS_RUN` (`PersistenceScope.CROSS_RUN`, or any dotted
    path ending there) — a name-based match, since AST can't resolve the
    attribute to the real enum member."""
    return isinstance(stmt.value, ast.Attribute) and stmt.value.attr == _CROSS_RUN_ATTR


def test_persisted_models_declare_scope() -> None:
    offenders = find_undeclared_scope_offenders(find_governed_files(__file__))
    assert not offenders, (
        "every PersistedModel subclass must declare "
        "SCOPE: ClassVar[PersistenceScope] in its class body "
        "(see app.core.persistence.PersistenceScope):\n  " + "\n  ".join(offenders)
    )


def test_cross_run_models_define_for_mode() -> None:
    offenders = find_cross_run_missing_for_mode_offenders(find_governed_files(__file__))
    assert not offenders, (
        "a PersistedModel with SCOPE = PersistenceScope.CROSS_RUN grants run "
        "activity a write that outlives the run, so it must also define "
        "for_mode, the view that revokes that write:\n  " + "\n  ".join(offenders)
    )


# --- unit tests for the checker, on inline snippets (red + green) ----------


def _write(tmp_path: Path, source: str) -> Path:
    target = tmp_path / "models.py"
    target.write_text(source)
    return target


def test_find_undeclared_scope_offenders_flags_a_subclass_missing_scope(tmp_path: Path) -> None:
    target = _write(
        tmp_path,
        "class Foo(PersistedModel):\n"
        "    collection: ClassVar[str] = 'foo'\n",
    )
    assert find_undeclared_scope_offenders([target]) == ["models.py:1  class Foo"]


def test_find_undeclared_scope_offenders_accepts_a_declared_scope(tmp_path: Path) -> None:
    target = _write(
        tmp_path,
        "class Foo(PersistedModel):\n"
        "    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.AUTHORED\n",
    )
    assert find_undeclared_scope_offenders([target]) == []


def test_find_undeclared_scope_offenders_rejects_a_bare_annotation_with_no_value(
    tmp_path: Path,
) -> None:
    """A bare `SCOPE: ClassVar[PersistenceScope]` (the base class's own
    declaration) assigns nothing, so it does not satisfy a subclass."""
    target = _write(
        tmp_path,
        "class Foo(PersistedModel):\n"
        "    SCOPE: ClassVar[PersistenceScope]\n",
    )
    assert find_undeclared_scope_offenders([target]) == ["models.py:1  class Foo"]


def test_find_undeclared_scope_offenders_ignores_a_non_persisted_model_class(
    tmp_path: Path,
) -> None:
    target = _write(tmp_path, "class Foo(BaseModel):\n    pass\n")
    assert find_undeclared_scope_offenders([target]) == []


def test_find_cross_run_missing_for_mode_offenders_flags_cross_run_without_for_mode(
    tmp_path: Path,
) -> None:
    target = _write(
        tmp_path,
        "class Foo(PersistedModel):\n"
        "    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.CROSS_RUN\n",
    )
    assert find_cross_run_missing_for_mode_offenders([target]) == ["models.py:1  class Foo"]


def test_find_cross_run_missing_for_mode_offenders_accepts_cross_run_with_for_mode(
    tmp_path: Path,
) -> None:
    target = _write(
        tmp_path,
        "class Foo(PersistedModel):\n"
        "    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.CROSS_RUN\n"
        "    def for_mode(self, mode):\n"
        "        pass\n",
    )
    assert find_cross_run_missing_for_mode_offenders([target]) == []


def test_find_cross_run_missing_for_mode_offenders_ignores_run_scope(tmp_path: Path) -> None:
    target = _write(
        tmp_path,
        "class Foo(PersistedModel):\n"
        "    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.RUN\n",
    )
    assert find_cross_run_missing_for_mode_offenders([target]) == []


def test_find_cross_run_missing_for_mode_offenders_ignores_a_class_missing_scope(
    tmp_path: Path,
) -> None:
    """A class that never declares SCOPE at all is `test_persisted_models_
    declare_scope`'s offender, not this test's — it has no CROSS_RUN
    assignment to react to."""
    target = _write(tmp_path, "class Foo(PersistedModel):\n    pass\n")
    assert find_cross_run_missing_for_mode_offenders([target]) == []


def test_assigns_cross_run_matches_the_enum_attribute() -> None:
    tree = ast.parse("SCOPE = PersistenceScope.CROSS_RUN\n")
    (stmt,) = tree.body
    assert isinstance(stmt, ast.Assign)
    assert _assigns_cross_run(stmt) is True


def test_assigns_cross_run_rejects_a_different_attribute() -> None:
    tree = ast.parse("SCOPE = PersistenceScope.AUTHORED\n")
    (stmt,) = tree.body
    assert isinstance(stmt, ast.Assign)
    assert _assigns_cross_run(stmt) is False

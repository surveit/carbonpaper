"""Architecture: every function in ``app/`` at or under cyclomatic complexity 20,
with no exception list. Scope deliberately INCLUDES ``_arch_tests/`` subdirs, which
``arch.scope`` exempts. ``cc_visit`` flattens methods but not closures, so this
module walks ``.closures`` itself; ``@typing.overload`` stubs are excluded before
`index_by_identity`, which otherwise fails loud on same-name collisions.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest
from radon.complexity import cc_visit
from radon.visitors import Function

from arch.scope import _EXEMPT_PARTS

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"
_COMPLEXITY_THRESHOLD = 20

# arch.scope's shared exemptions (tests/, __pycache__/, _vendor/, node_modules/,
# venv/, ...), minus "_arch_tests" — this rule is the one exception that keeps
# scanning _arch_tests/ (see the module docstring). Reused rather than
# duplicated so a new shared exemption (e.g. a future vendoring convention)
# does not have to be added here too.
_SOURCE_EXEMPT_PARTS = _EXEMPT_PARTS - {"_arch_tests"}


@dataclass(frozen=True)
class FunctionComplexity:
    """One function/method/closure's measured cyclomatic complexity.

    ``path`` is repo-relative with forward slashes (identical on Windows and
    CI Linux); ``function`` is the qualified name described in the module
    docstring.
    """

    path: str
    line: int
    function: str
    complexity: int


def find_app_source_files(root: Path) -> list[Path]:
    """Every ``.py`` file under `root`, except a dot-directory or one of
    `_SOURCE_EXEMPT_PARTS` (``arch.scope``'s own exemptions, so third-party
    code vendored under ``app/_vendor/`` is never held to this rule) — the
    one difference from ``arch.scope`` is that ``_arch_tests/`` stays in
    scope here (see the module docstring). A part is checked on the path
    relative to `root`, not the absolute path: this worktree can itself live
    under a dot-directory (e.g. a git worktree under ``.claude/``), and
    checking the absolute path would spuriously exempt everything under it."""
    files = [
        path
        for path in sorted(root.rglob("*.py"))
        if not any(
            part.startswith(".") or part in _SOURCE_EXEMPT_PARTS for part in path.relative_to(root).parts
        )
    ]
    if not files:
        raise ValueError(f"complexity ratchet governs no source files under {root}")
    return files


def measure_function_complexities(paths: list[Path], repo_root: Path) -> list[FunctionComplexity]:
    """Every function, method, and closure in `paths`, at its measured
    complexity — except an `@typing.overload` stub (see `find_overload_stub_lines`),
    which is never included: its body is always the trivial `...`, so admitting
    it would only create a same-name collision for `index_by_identity` to
    (correctly) reject, without protecting against anything."""
    measurements: list[FunctionComplexity] = []
    for path in paths:
        rel_path = path.relative_to(repo_root).as_posix()
        text = path.read_text(encoding="utf-8")
        overload_lines = find_overload_stub_lines(text)
        for block in cc_visit(text):
            if isinstance(block, Function):
                measurements.extend(
                    _measure_function_and_closures(block, rel_path, parent=None, overload_lines=overload_lines)
                )
    return measurements


def find_overload_stub_lines(text: str) -> set[int]:
    """Line numbers of every function/method def in `text` decorated with
    `overload` — bare (`@overload`) or dotted (`@typing.overload`). radon's own
    `Function.lineno` for a def is the same line ast reports here (the `def`
    line, not the decorator), so a caller can match these lines against radon's
    measurements directly."""
    tree = ast.parse(text)
    return {
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _has_overload_decorator(node)
    }


def _has_overload_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Name) and decorator.id == "overload":
            return True
        if isinstance(decorator, ast.Attribute) and decorator.attr == "overload":
            return True
    return False


def find_functions_over_threshold(measurements: list[FunctionComplexity]) -> list[str]:
    """Human-readable offender lines for every function in `measurements`
    whose complexity exceeds `_COMPLEXITY_THRESHOLD` — there is no exception
    list, so this is the whole rule."""
    by_key = index_by_identity(measurements, source="the measured functions")
    return [_describe_violation(by_key[key]) for key in sorted(by_key) if by_key[key].complexity > _COMPLEXITY_THRESHOLD]


def index_by_identity(measurements: list[FunctionComplexity], *, source: str) -> dict[tuple[str, str], FunctionComplexity]:
    """`measurements` keyed by ``(path, function)``, raising loudly on a
    duplicate identity instead of silently keeping the last one. Two blocks
    that resolve to the same qualified name — ``@typing.overload`` stubs, or
    a platform-conditional ``def foo(): ... / def foo(): ...`` under
    ``if``/``else`` — would otherwise collapse into one dict entry and drop
    the other's complexity unnoticed, which could let a real violator slip
    past silently instead of failing loud."""
    by_key: dict[tuple[str, str], FunctionComplexity] = {}
    for m in measurements:
        key = (m.path, m.function)
        if key in by_key:
            raise ValueError(
                f"{source}: two entries for {m.path}:{m.function} (lines {by_key[key].line} "
                f"and {m.line}) — the complexity ratchet keys by (path, function) and cannot "
                "tell them apart; give one of them a distinct name, or — for @typing.overload "
                "stubs, where a distinct name is not an option — amend this checker's identity "
                "key in tests/arch/test_complexity_ratchet.py"
            )
        by_key[key] = m
    return by_key


def test_functions_do_not_exceed_the_complexity_ratchet() -> None:
    measurements = measure_function_complexities(find_app_source_files(_APP_ROOT), _REPO_ROOT)
    offenders = find_functions_over_threshold(measurements)
    assert not offenders, (
        f"cyclomatic complexity ratchet: every function in app/ must measure at or under "
        f"complexity {_COMPLEXITY_THRESHOLD}, with no exception list — decompose it:\n  "
        + "\n  ".join(offenders)
    )


# --- qualified-name walk -----------------------------------------------------


def _measure_function_and_closures(
    func: Function, rel_path: str, parent: str | None, overload_lines: set[int]
) -> list[FunctionComplexity]:
    """`func` and every closure nested inside it, recursively, each as a
    `FunctionComplexity` — except `func` itself when its line is one of
    `overload_lines` (an `@typing.overload` stub), which is dropped."""
    name = func.fullname if parent is None else f"{parent}.<{func.name}>"
    measurements = (
        [] if func.lineno in overload_lines
        else [FunctionComplexity(rel_path, func.lineno, name, func.complexity)]
    )
    for closure in func.closures:
        measurements.extend(
            _measure_function_and_closures(closure, rel_path, parent=name, overload_lines=overload_lines)
        )
    return measurements


# --- offender messages --------------------------------------------------------


def _describe_violation(m: FunctionComplexity) -> str:
    return (
        f"{m.path}:{m.line}  {m.function}  complexity={m.complexity} "
        f"(> {_COMPLEXITY_THRESHOLD}) — decompose it"
    )


# --- unit tests for the checker, on inline snippets (red + green) -----------


def test_find_app_source_files_includes_arch_tests_subdir(tmp_path: Path) -> None:
    (tmp_path / "_arch_tests").mkdir()
    (tmp_path / "_arch_tests" / "test_x.py").write_text("", encoding="utf-8")
    (tmp_path / "mod.py").write_text("", encoding="utf-8")
    files = find_app_source_files(tmp_path)
    assert {path.name for path in files} == {"test_x.py", "mod.py"}


def test_find_app_source_files_excludes_pycache(tmp_path: Path) -> None:
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "mod.py").write_text("", encoding="utf-8")
    (tmp_path / "mod.py").write_text("", encoding="utf-8")
    files = find_app_source_files(tmp_path)
    assert [path.name for path in files] == ["mod.py"]


def test_find_app_source_files_excludes_vendor_but_includes_arch_tests(tmp_path: Path) -> None:
    """Third-party code vendored under `_vendor/` (one of `arch.scope`'s
    shared exemptions) must never be held to this rule, while
    `_arch_tests/` — this rule's one deliberate difference from
    `arch.scope` — stays in scope."""
    (tmp_path / "_vendor").mkdir()
    (tmp_path / "_vendor" / "third_party.py").write_text("", encoding="utf-8")
    (tmp_path / "_arch_tests").mkdir()
    (tmp_path / "_arch_tests" / "test_x.py").write_text("", encoding="utf-8")
    (tmp_path / "mod.py").write_text("", encoding="utf-8")
    files = find_app_source_files(tmp_path)
    assert {path.name for path in files} == {"test_x.py", "mod.py"}


def test_find_app_source_files_ignores_a_dot_directory_in_the_scanned_root_prefix(tmp_path: Path) -> None:
    """Exemptions are checked on the path relative to `root`, not the
    absolute path: this repo's own worktrees live under `.claude/`, so a
    `root` whose absolute prefix contains a dot-directory must not
    spuriously exempt everything underneath it (the same concern
    `arch.scope`'s module docstring documents for its own scan)."""
    root = tmp_path / ".claude" / "worktrees" / "x" / "app"
    root.mkdir(parents=True)
    (root / "mod.py").write_text("", encoding="utf-8")
    files = find_app_source_files(root)
    assert [path.name for path in files] == ["mod.py"]


def test_find_app_source_files_excludes_a_dot_directory_inside_the_scanned_tree(tmp_path: Path) -> None:
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "mod.py").write_text("", encoding="utf-8")
    (tmp_path / "mod.py").write_text("", encoding="utf-8")
    files = find_app_source_files(tmp_path)
    assert [path.name for path in files] == ["mod.py"]


def test_find_app_source_files_raises_when_root_has_no_python_files(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="governs no source files"):
        find_app_source_files(tmp_path)


def test_measure_function_complexities_reads_relative_posix_path(tmp_path: Path) -> None:
    nested = tmp_path / "app" / "sub"
    nested.mkdir(parents=True)
    file = nested / "m.py"
    file.write_text("def go(x):\n    if x:\n        return 1\n    return 2\n", encoding="utf-8")
    [measurement] = measure_function_complexities([file], tmp_path)
    assert measurement.path == "app/sub/m.py"
    assert measurement.function == "go"
    assert measurement.complexity == 2


def test_measure_function_complexities_qualifies_a_method_as_class_dot_method(tmp_path: Path) -> None:
    file = tmp_path / "m.py"
    file.write_text("class Foo:\n    def bar(self, x):\n        if x:\n            return 1\n        return 2\n", encoding="utf-8")
    [measurement] = measure_function_complexities([file], tmp_path)
    assert measurement.function == "Foo.bar"


def test_measure_function_complexities_qualifies_a_closure_as_parent_dot_angle_name(tmp_path: Path) -> None:
    file = tmp_path / "m.py"
    file.write_text(
        "def outer(x):\n"
        "    def inner(y):\n"
        "        if y:\n"
        "            return 1\n"
        "        return 2\n"
        "    return inner(x)\n"
    , encoding="utf-8")
    measurements = measure_function_complexities([file], tmp_path)
    assert {m.function for m in measurements} == {"outer", "outer.<inner>"}


def test_measure_function_complexities_qualifies_nested_closures_recursively(tmp_path: Path) -> None:
    file = tmp_path / "m.py"
    file.write_text(
        "def outer():\n"
        "    def inner():\n"
        "        def innermost(z):\n"
        "            if z:\n"
        "                return 1\n"
        "            return 2\n"
        "        return innermost\n"
        "    return inner\n"
    , encoding="utf-8")
    measurements = measure_function_complexities([file], tmp_path)
    assert {m.function for m in measurements} == {"outer", "outer.<inner>", "outer.<inner>.<innermost>"}


def test_find_overload_stub_lines_detects_bare_overload(tmp_path: Path) -> None:
    text = (
        "from typing import overload\n"
        "@overload\n"
        "def go(x: int) -> int: ...\n"
        "@overload\n"
        "def go(x: str) -> str: ...\n"
        "def go(x):\n"
        "    return x\n"
    )
    assert find_overload_stub_lines(text) == {3, 5}


def test_find_overload_stub_lines_detects_dotted_overload(tmp_path: Path) -> None:
    text = (
        "import typing\n"
        "@typing.overload\n"
        "def go(x: int) -> int: ...\n"
        "def go(x):\n"
        "    return x\n"
    )
    assert find_overload_stub_lines(text) == {3}


def test_find_overload_stub_lines_ignores_undecorated_functions() -> None:
    assert find_overload_stub_lines("def go(x):\n    return x\n") == set()


def test_measure_function_complexities_excludes_overload_stubs_but_keeps_the_implementation(
    tmp_path: Path,
) -> None:
    """The bug this guards: an `@typing.overload` group's stubs and its real
    implementation all resolve to the same qualified name, which would
    otherwise make `index_by_identity` raise on every overloaded method in
    app/ — a false positive this measurement-layer exclusion prevents by never
    handing the (trivial, always-`...`-bodied) stubs to it in the first
    place."""
    file = tmp_path / "m.py"
    file.write_text(
        "from typing import overload\n"
        "class Foo:\n"
        "    @overload\n"
        "    def go(self, x: int) -> int: ...\n"
        "    @overload\n"
        "    def go(self, x: str) -> str: ...\n"
        "    def go(self, x):\n"
        "        if x:\n"
        "            return 1\n"
        "        return 2\n"
    , encoding="utf-8")
    measurements = measure_function_complexities([file], tmp_path)
    assert [(m.function, m.line) for m in measurements] == [("Foo.go", 7)]


def test_measure_function_complexities_qualifies_a_closure_inside_a_method(tmp_path: Path) -> None:
    file = tmp_path / "m.py"
    file.write_text(
        "class Foo:\n"
        "    def bar(self, x):\n"
        "        def inner(y):\n"
        "            if y:\n"
        "                return 1\n"
        "            return 2\n"
        "        return inner(x)\n"
    , encoding="utf-8")
    measurements = measure_function_complexities([file], tmp_path)
    assert {m.function for m in measurements} == {"Foo.bar", "Foo.bar.<inner>"}


def test_measure_function_complexities_surfaces_both_blocks_of_a_platform_conditional_duplicate_name(
    tmp_path: Path,
) -> None:
    """Two ``def foo():`` under ``if``/``else`` (or, equivalently,
    ``@typing.overload`` stubs) both resolve to the same qualified name —
    radon reports both as separate blocks rather than collapsing them, so the
    measurement layer must surface both too; it is `index_by_identity`
    downstream that must then refuse to silently pick one."""
    file = tmp_path / "m.py"
    file.write_text(
        "if True:\n"
        "    def foo(x):\n"
        "        if x:\n"
        "            return 1\n"
        "        return 2\n"
        "else:\n"
        "    def foo(x):\n"
        "        return 3\n"
    , encoding="utf-8")
    measurements = measure_function_complexities([file], tmp_path)
    assert [(m.function, m.line) for m in measurements] == [("foo", 2), ("foo", 7)]


def _make_measurement(path: str = "a.py", line: int = 1, function: str = "go", complexity: int = 25) -> FunctionComplexity:
    return FunctionComplexity(path=path, line=line, function=function, complexity=complexity)


def test_find_functions_over_threshold_flags_a_violator() -> None:
    offenders = find_functions_over_threshold([_make_measurement(complexity=25)])
    assert len(offenders) == 1
    assert "a.py:1" in offenders[0] and "decompose" in offenders[0]


def test_find_functions_over_threshold_ignores_a_function_at_or_below_threshold() -> None:
    assert find_functions_over_threshold([_make_measurement(complexity=_COMPLEXITY_THRESHOLD)]) == []


def test_index_by_identity_raises_on_two_measurements_for_the_same_identity() -> None:
    duplicates = [
        _make_measurement(path="a.py", function="go", line=1, complexity=25),
        _make_measurement(path="a.py", function="go", line=10, complexity=30),
    ]
    with pytest.raises(ValueError, match="a.py:go"):
        index_by_identity(duplicates, source="test")


def test_index_by_identity_raise_offers_a_remedy_for_the_unrenamable_overload_case() -> None:
    """`@typing.overload` stubs are named as an expected trigger for this
    collision, and a stub cannot simply be renamed — the message must offer
    a remedy that is actually possible for that case, not just "give it a
    distinct name"."""
    duplicates = [
        _make_measurement(path="a.py", function="go", line=1, complexity=25),
        _make_measurement(path="a.py", function="go", line=10, complexity=30),
    ]
    with pytest.raises(ValueError, match="distinct name") as excinfo:
        index_by_identity(duplicates, source="test")
    assert "overload" in str(excinfo.value)


def test_index_by_identity_keeps_two_different_functions_in_the_same_file() -> None:
    distinct = [
        _make_measurement(path="a.py", function="go", complexity=25),
        _make_measurement(path="a.py", function="stop", complexity=25),
    ]
    assert set(index_by_identity(distinct, source="test")) == {("a.py", "go"), ("a.py", "stop")}


def test_find_functions_over_threshold_raises_on_two_measurements_for_the_same_identity() -> None:
    """The bug this guards: without a loud raise, a dict comprehension keyed
    by (path, function) would silently keep only the LAST of two same-named
    blocks — e.g. a platform-conditional `def foo(): / def foo():` under
    `if`/`else` — and a genuinely violating one could be the one dropped,
    letting it slip past silently instead of failing loud."""
    duplicates = [
        _make_measurement(path="a.py", function="go", line=1, complexity=25),
        _make_measurement(path="a.py", function="go", line=10, complexity=5),
    ]
    with pytest.raises(ValueError, match="a.py:go"):
        find_functions_over_threshold(duplicates)

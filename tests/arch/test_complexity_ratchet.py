"""Architecture: a cyclomatic-complexity ratchet on ``app/``.

Cyclomatic complexity above 20 (radon grade D or worse) is a function too
tangled to review confidently — this rule blocks new ones and only lets
existing ones shrink. Today's offenders are grandfathered in the committed
``complexity_baseline.json`` next to this test; the ratchet enforces three
things:

1. A violating function (complexity > 20) not recorded in the baseline is a
   new offender — decompose it.
2. A baselined function whose measured complexity no longer matches its
   recorded value is a drift: higher is a regression (decompose it back
   down), lower is an improvement that has not been locked in (update the
   recorded value so the baseline actually shrinks).
3. A baseline entry for a function that no longer exists, or that now
   measures at or under 20, is stale — remove the entry. A baseline that
   only ever grows is not a ratchet.

Scope is every ``.py`` file under ``app/``, including ``_arch_tests/``
subdirs — unlike ``arch.scope``, which exempts them (they hold other rules'
own inline fixtures, not code this rule needs to skip). Nothing under
``tests/`` is scanned, matching every other arch rule.

Complexity is measured with radon as a library (``radon.complexity.cc_visit``),
not a subprocess, so a worktree with no radon on PATH still runs this test via
the installed dependency. A method's qualified name is radon's own
``ClassName.method``; a closure's is its parent's qualified name plus
``.<name>``, so a closure nested two levels deep reads as
``outer.<inner>.<innermost>`` — ``cc_visit`` does not flatten closures the way
it flattens methods, so this module walks ``.closures`` itself, recursively.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from radon.complexity import cc_visit
from radon.visitors import Function

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"
_BASELINE_PATH = Path(__file__).resolve().parent / "complexity_baseline.json"
_COMPLEXITY_THRESHOLD = 20


@dataclass(frozen=True)
class FunctionComplexity:
    """One function/method/closure's measured cyclomatic complexity.

    ``path`` is repo-relative with forward slashes (identical on Windows and
    CI Linux); ``function`` is the qualified name described in the module
    docstring. The same shape serves both a live measurement and a baseline
    entry, so the two compare field-for-field.
    """

    path: str
    line: int
    function: str
    complexity: int


def find_app_source_files(root: Path) -> list[Path]:
    """Every ``.py`` file under `root`, including ``_arch_tests/`` subdirs
    (see the module docstring for why this rule does not use
    ``arch.scope``'s exemptions). Excludes only ``__pycache__``."""
    files = [path for path in sorted(root.rglob("*.py")) if "__pycache__" not in path.parts]
    if not files:
        raise ValueError(f"complexity ratchet governs no source files under {root}")
    return files


def measure_function_complexities(paths: list[Path], repo_root: Path) -> list[FunctionComplexity]:
    """Every function, method, and closure in `paths`, at its measured
    complexity — not just violators, so a caller can also look up whether a
    baselined function has dropped below the threshold."""
    measurements: list[FunctionComplexity] = []
    for path in paths:
        rel_path = path.relative_to(repo_root).as_posix()
        text = path.read_text(encoding="utf-8")
        for block in cc_visit(text):
            if isinstance(block, Function):
                measurements.extend(_measure_function_and_closures(block, rel_path, parent=None))
    return measurements


def find_ratchet_violations(
    measurements: list[FunctionComplexity],
    baseline: dict[tuple[str, str], FunctionComplexity],
) -> list[str]:
    """Human-readable offender lines for the three ratchet rules in the
    module docstring, run over `measurements` against `baseline`."""
    by_key = index_by_identity(measurements, source="the measured functions")
    violator_keys = {key for key, m in by_key.items() if m.complexity > _COMPLEXITY_THRESHOLD}
    baseline_keys = set(baseline)

    offenders = [_describe_new_violation(by_key[key]) for key in sorted(violator_keys - baseline_keys)]
    offenders += [
        _describe_drift(by_key[key], baseline[key])
        for key in sorted(violator_keys & baseline_keys)
        if by_key[key].complexity != baseline[key].complexity
    ]
    offenders += [
        _describe_stale_entry(baseline[key], by_key.get(key)) for key in sorted(baseline_keys - violator_keys)
    ]
    return offenders


def index_by_identity(measurements: list[FunctionComplexity], *, source: str) -> dict[tuple[str, str], FunctionComplexity]:
    """`measurements` keyed by ``(path, function)``, raising loudly on a
    duplicate identity instead of silently keeping the last one. Two blocks
    that resolve to the same qualified name — ``@typing.overload`` stubs, or
    a platform-conditional ``def foo(): ... / def foo(): ...`` under
    ``if``/``else`` — would otherwise collapse into one dict entry and drop
    the other's complexity unnoticed, which would let a real violator slip
    past ratchet rule 1 (a new violator must always fail)."""
    by_key: dict[tuple[str, str], FunctionComplexity] = {}
    for m in measurements:
        key = (m.path, m.function)
        if key in by_key:
            raise ValueError(
                f"{source}: two entries for {m.path}:{m.function} (lines {by_key[key].line} "
                f"and {m.line}) — the complexity ratchet keys by (path, function) and cannot "
                "tell them apart; give one of them a distinct name"
            )
        by_key[key] = m
    return by_key


def load_baseline(path: Path) -> dict[tuple[str, str], FunctionComplexity]:
    """The committed baseline as ``{(path, function): entry}``. A missing
    file reads as an empty baseline — the legitimate starting state before
    any function was ever grandfathered in — rather than an error. A
    baseline entry's ``line`` is not part of its identity (only `path` and
    `function` are, so the entry stays matched across an unrelated line
    shift elsewhere in the file); it can go stale and is informational only.
    Raises loudly (see `index_by_identity`) on two entries for the same
    identity rather than silently keeping the last one."""
    if not path.exists():
        return {}
    entries = json.loads(path.read_text(encoding="utf-8"))
    return index_by_identity(
        [FunctionComplexity(**entry) for entry in entries], source=f"baseline {path}"
    )


def test_functions_do_not_exceed_the_complexity_ratchet() -> None:
    baseline = load_baseline(_BASELINE_PATH)
    measurements = measure_function_complexities(find_app_source_files(_APP_ROOT), _REPO_ROOT)
    offenders = find_ratchet_violations(measurements, baseline)
    assert not offenders, (
        "cyclomatic complexity ratchet: a function over complexity 20 must be decomposed, "
        "or — if pre-existing — recorded exactly in tests/arch/complexity_baseline.json; the "
        "baseline may only shrink, never grow, and every entry must match today's measured "
        "complexity for a function still above 20:\n  " + "\n  ".join(offenders)
    )


# --- qualified-name walk -----------------------------------------------------


def _measure_function_and_closures(func: Function, rel_path: str, parent: str | None) -> list[FunctionComplexity]:
    """`func` and every closure nested inside it, recursively, each as a
    `FunctionComplexity`."""
    name = func.fullname if parent is None else f"{parent}.<{func.name}>"
    measurements = [FunctionComplexity(rel_path, func.lineno, name, func.complexity)]
    for closure in func.closures:
        measurements.extend(_measure_function_and_closures(closure, rel_path, parent=name))
    return measurements


# --- offender messages --------------------------------------------------------


def _describe_new_violation(m: FunctionComplexity) -> str:
    return (
        f"{m.path}:{m.line}  {m.function}  complexity={m.complexity} (> 20, not in the "
        "baseline) — decompose it; the baseline must never grow"
    )


def _describe_drift(current: FunctionComplexity, recorded: FunctionComplexity) -> str:
    if current.complexity > recorded.complexity:
        verdict = f"regressed from {recorded.complexity} — decompose it back down"
    else:
        verdict = (
            f"improved from {recorded.complexity} — update complexity_baseline.json to "
            f"{current.complexity} so the improvement locks in"
        )
    return f"{current.path}:{current.line}  {current.function}  complexity={current.complexity} ({verdict})"


def _describe_stale_entry(recorded: FunctionComplexity, current: FunctionComplexity | None) -> str:
    reason = "the function no longer exists" if current is None else f"it now measures {current.complexity} (<= 20)"
    return (
        f"{recorded.path}:{recorded.line}  {recorded.function}  baseline complexity="
        f"{recorded.complexity} ({reason}) — remove the stale baseline entry"
    )


# --- unit tests for the checker, on inline snippets (red + green) -----------


def test_find_app_source_files_includes_arch_tests_subdir(tmp_path: Path) -> None:
    (tmp_path / "_arch_tests").mkdir()
    (tmp_path / "_arch_tests" / "test_x.py").write_text("")
    (tmp_path / "mod.py").write_text("")
    files = find_app_source_files(tmp_path)
    assert {path.name for path in files} == {"test_x.py", "mod.py"}


def test_find_app_source_files_excludes_pycache(tmp_path: Path) -> None:
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "mod.py").write_text("")
    (tmp_path / "mod.py").write_text("")
    files = find_app_source_files(tmp_path)
    assert [path.name for path in files] == ["mod.py"]


def test_find_app_source_files_raises_when_root_has_no_python_files(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="governs no source files"):
        find_app_source_files(tmp_path)


def test_measure_function_complexities_reads_relative_posix_path(tmp_path: Path) -> None:
    nested = tmp_path / "app" / "sub"
    nested.mkdir(parents=True)
    file = nested / "m.py"
    file.write_text("def go(x):\n    if x:\n        return 1\n    return 2\n")
    [measurement] = measure_function_complexities([file], tmp_path)
    assert measurement.path == "app/sub/m.py"
    assert measurement.function == "go"
    assert measurement.complexity == 2


def test_measure_function_complexities_qualifies_a_method_as_class_dot_method(tmp_path: Path) -> None:
    file = tmp_path / "m.py"
    file.write_text("class Foo:\n    def bar(self, x):\n        if x:\n            return 1\n        return 2\n")
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
    )
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
    )
    measurements = measure_function_complexities([file], tmp_path)
    assert {m.function for m in measurements} == {"outer", "outer.<inner>", "outer.<inner>.<innermost>"}


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
    )
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
    )
    measurements = measure_function_complexities([file], tmp_path)
    assert [(m.function, m.line) for m in measurements] == [("foo", 2), ("foo", 7)]


def test_load_baseline_reads_a_missing_file_as_empty(tmp_path: Path) -> None:
    assert load_baseline(tmp_path / "missing.json") == {}


def test_load_baseline_keys_entries_by_path_and_function(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps([{"path": "app/a.py", "line": 3, "function": "go", "complexity": 25}]))
    assert load_baseline(baseline_path) == {("app/a.py", "go"): FunctionComplexity("app/a.py", 3, "go", 25)}


def test_load_baseline_raises_on_two_entries_for_the_same_identity(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps(
            [
                {"path": "app/a.py", "line": 3, "function": "go", "complexity": 25},
                {"path": "app/a.py", "line": 30, "function": "go", "complexity": 26},
            ]
        )
    )
    with pytest.raises(ValueError, match="app/a.py:go"):
        load_baseline(baseline_path)


def _make_measurement(path: str = "a.py", line: int = 1, function: str = "go", complexity: int = 25) -> FunctionComplexity:
    return FunctionComplexity(path=path, line=line, function=function, complexity=complexity)


def test_find_ratchet_violations_flags_an_unbaselined_violator() -> None:
    offenders = find_ratchet_violations([_make_measurement(complexity=25)], {})
    assert len(offenders) == 1
    assert "a.py:1" in offenders[0] and "decompose" in offenders[0]


def test_find_ratchet_violations_passes_a_violator_matching_the_baseline_exactly() -> None:
    measurement = _make_measurement(complexity=25)
    baseline = {("a.py", "go"): measurement}
    assert find_ratchet_violations([measurement], baseline) == []


def test_find_ratchet_violations_ignores_a_function_at_or_below_threshold_with_no_baseline() -> None:
    assert find_ratchet_violations([_make_measurement(complexity=_COMPLEXITY_THRESHOLD)], {}) == []


def test_find_ratchet_violations_flags_a_regression_above_the_recorded_value() -> None:
    baseline = {("a.py", "go"): _make_measurement(complexity=25)}
    offenders = find_ratchet_violations([_make_measurement(complexity=30)], baseline)
    assert len(offenders) == 1
    assert "regressed" in offenders[0]


def test_find_ratchet_violations_flags_an_improvement_below_the_recorded_value() -> None:
    baseline = {("a.py", "go"): _make_measurement(complexity=25)}
    offenders = find_ratchet_violations([_make_measurement(complexity=22)], baseline)
    assert len(offenders) == 1
    assert "improved" in offenders[0] and "lock" in offenders[0]


def test_find_ratchet_violations_flags_a_stale_entry_when_the_function_is_gone() -> None:
    baseline = {("a.py", "go"): _make_measurement(complexity=25)}
    offenders = find_ratchet_violations([], baseline)
    assert len(offenders) == 1
    assert "no longer exists" in offenders[0] and "remove" in offenders[0]


def test_find_ratchet_violations_flags_a_stale_entry_when_now_below_threshold() -> None:
    baseline = {("a.py", "go"): _make_measurement(complexity=25)}
    offenders = find_ratchet_violations([_make_measurement(complexity=15)], baseline)
    assert len(offenders) == 1
    assert "<= 20" in offenders[0] and "remove" in offenders[0]


def test_index_by_identity_raises_on_two_measurements_for_the_same_identity() -> None:
    duplicates = [
        _make_measurement(path="a.py", function="go", line=1, complexity=25),
        _make_measurement(path="a.py", function="go", line=10, complexity=30),
    ]
    with pytest.raises(ValueError, match="a.py:go"):
        index_by_identity(duplicates, source="test")


def test_index_by_identity_keeps_two_different_functions_in_the_same_file() -> None:
    distinct = [
        _make_measurement(path="a.py", function="go", complexity=25),
        _make_measurement(path="a.py", function="stop", complexity=25),
    ]
    assert set(index_by_identity(distinct, source="test")) == {("a.py", "go"), ("a.py", "stop")}


def test_find_ratchet_violations_raises_on_two_measurements_for_the_same_identity() -> None:
    """The bug this guards: without a loud raise, a dict comprehension keyed
    by (path, function) would silently keep only the LAST of two same-named
    blocks — e.g. a platform-conditional `def foo(): / def foo():` under
    `if`/`else` — and a genuinely violating one could be the one dropped,
    letting ratchet rule 1 (a new violator must always fail) slip past
    silently instead of failing loud."""
    duplicates = [
        _make_measurement(path="a.py", function="go", line=1, complexity=25),
        _make_measurement(path="a.py", function="go", line=10, complexity=5),
    ]
    with pytest.raises(ValueError, match="a.py:go"):
        find_ratchet_violations(duplicates, {})

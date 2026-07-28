"""Architecture: every module docstring in ``app/`` and ``tests/`` at or under 5
physical lines, no baseline; `_JUSTIFIED_EXCEPTIONS` (symbol-keyed, reason-mandatory,
empty today) is meant to stay very rare — normally cut the docstring or move the
content to docs/. Blank lines inside the docstring count, so a two-paragraph one
measures over the ceiling by design: the rule forces one contiguous block.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from arch.test_complexity_ratchet import _SOURCE_EXEMPT_PARTS, find_app_source_files

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"
_TESTS_ROOT = _REPO_ROOT / "tests"
_DOCSTRING_LINE_CEILING = 5

# The complexity ratchet's exemptions, minus "tests" — this rule governs the test
# tree too, so a directory named `tests` inside it must stay in scope.
_TESTS_EXEMPT_PARTS = _SOURCE_EXEMPT_PARTS - {"tests"}

# Modules whose docstring is allowed past the ceiling, each mapped to the written
# reason it earned that. Keyed on the SYMBOL — here the repo-relative posix module
# path — never a line number, which would rot on the next unrelated edit; a
# symbol-keyed entry survives until the symbol is renamed or deleted, at which point
# it fails loud as stale. A future function-level rule keys the same way, with
# `path::Qualified.name`. Entries may only be removed, never added: an exception here
# should be very rare, and the normal remedy is cutting the docstring or moving the
# content to docs/. Empty — the sweep that introduced this rule cleared every
# offender, so the mechanism ships unused on purpose.
_JUSTIFIED_EXCEPTIONS: dict[str, str] = {}


@dataclass(frozen=True)
class ModuleDocstring:
    """``path`` is repo-relative with forward slashes (identical on Windows and CI
    Linux); ``lines`` counts physical lines of the raw docstring text, blank lines
    included."""

    path: str
    lines: int


def find_governed_files(app_root: Path, tests_root: Path) -> list[Path]:
    return find_app_source_files(app_root) + find_python_files(tests_root)


def find_python_files(root: Path) -> list[Path]:
    """Recursive, minus `_TESTS_EXEMPT_PARTS` (derived from the complexity ratchet's
    set, so a future shared exemption reaches both scanners). Parts are checked on
    the path relative to `root`: this repo's worktrees can live under a
    dot-directory, whose absolute parts would spuriously exempt the whole tree."""
    files = [
        path
        for path in sorted(root.rglob("*.py"))
        if not any(
            part.startswith(".") or part in _TESTS_EXEMPT_PARTS
            for part in path.relative_to(root).parts
        )
    ]
    if not files:
        raise ValueError(f"module-docstring ratchet governs no source files under {root}")
    return files


def measure_module_docstrings(paths: list[Path], repo_root: Path) -> list[ModuleDocstring]:
    """Only files that HAVE a module docstring; a file without one is absent from
    the result rather than measured as zero."""
    measurements = []
    for path in paths:
        docstring = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")), clean=False)
        if docstring is not None:
            measurements.append(
                ModuleDocstring(path.relative_to(repo_root).as_posix(), len(docstring.splitlines()))
            )
    return measurements


def find_ratchet_violations(
    measurements: list[ModuleDocstring], exceptions: dict[str, str]
) -> list[str]:
    """Offender lines for two rules: a module over the ceiling and not in
    `exceptions`, and an `exceptions` entry that no longer describes an
    over-ceiling module (stale — the ratchet may only shrink)."""
    by_path = {measurement.path: measurement for measurement in measurements}
    offenders = [
        _describe_violation(by_path[path])
        for path in sorted(by_path)
        if by_path[path].lines > _DOCSTRING_LINE_CEILING and path not in exceptions
    ]
    offenders += [
        _describe_stale_exception(path)
        for path in sorted(exceptions)
        if path not in by_path or by_path[path].lines <= _DOCSTRING_LINE_CEILING
    ]
    return offenders


def test_module_docstrings_do_not_exceed_the_ratchet() -> None:
    measurements = measure_module_docstrings(
        find_governed_files(_APP_ROOT, _TESTS_ROOT), _REPO_ROOT
    )
    offenders = find_ratchet_violations(measurements, _JUSTIFIED_EXCEPTIONS)
    assert not offenders, (
        f"module-docstring ratchet (every module under app/ and tests/): a module "
        f"docstring must be at most {_DOCSTRING_LINE_CEILING} physical lines, with no "
        "baseline. Write one line of what's in the file, plus at most the one or two "
        "lines carrying a real gotcha or non-obvious invariant; architecture narration "
        "belongs in docs/, not atop a module. Blank lines INSIDE the docstring count, so "
        "a docstring of two short paragraphs can measure 6 — that is intended, the rule "
        "forces one contiguous block. Cutting the docstring (or moving the content to "
        "docs/) is the normal remedy: adding a module path to the _JUSTIFIED_EXCEPTIONS "
        "dict in tests/arch/test_module_docstring_ratchet.py, with a written reason, "
        "should be very rare, and that dict may only shrink:\n  " + "\n  ".join(offenders)
    )


# --- offender messages -----------------------------------------------------


def _describe_violation(measurement: ModuleDocstring) -> str:
    return (
        f"{measurement.path}  docstring_lines={measurement.lines} "
        f"(> {_DOCSTRING_LINE_CEILING}, not in _JUSTIFIED_EXCEPTIONS) — cut it down"
    )


def _describe_stale_exception(path: str) -> str:
    return (
        f"{path}  (no longer an over-ceiling module docstring — the module was deleted or "
        "renamed, or its docstring is already at or under the ceiling) — delete the stale "
        "_JUSTIFIED_EXCEPTIONS entry"
    )


# --- unit tests for the checker, on tmp_path fixtures (red + green) -------


def _write_module(tmp_path: Path, body: str, name: str = "m.py") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_measure_module_docstrings_flags_a_six_line_docstring(tmp_path: Path) -> None:
    file = _write_module(tmp_path, '"""' + "\n".join(f"line {n}" for n in range(6)) + '\n"""\n')
    [measurement] = measure_module_docstrings([file], tmp_path)
    assert measurement.lines == 6
    offenders = find_ratchet_violations([measurement], {})
    assert len(offenders) == 1
    assert "m.py" in offenders[0] and "cut it down" in offenders[0]


def test_measure_module_docstrings_passes_a_five_line_docstring(tmp_path: Path) -> None:
    file = _write_module(tmp_path, '"""' + "\n".join(f"line {n}" for n in range(5)) + '\n"""\n')
    [measurement] = measure_module_docstrings([file], tmp_path)
    assert measurement.lines == _DOCSTRING_LINE_CEILING
    assert find_ratchet_violations([measurement], {}) == []


def test_measure_module_docstrings_passes_a_one_line_docstring(tmp_path: Path) -> None:
    file = _write_module(tmp_path, '"""What is in here."""\n')
    [measurement] = measure_module_docstrings([file], tmp_path)
    assert measurement.lines == 1


def test_measure_module_docstrings_skips_a_file_with_no_docstring(tmp_path: Path) -> None:
    file = _write_module(tmp_path, "x = 1\n")
    assert measure_module_docstrings([file], tmp_path) == []


def test_measure_module_docstrings_skips_a_file_whose_first_string_is_not_a_docstring(
    tmp_path: Path,
) -> None:
    file = _write_module(tmp_path, "x = 1\n'''not a module docstring'''\n")
    assert measure_module_docstrings([file], tmp_path) == []


def test_measure_module_docstrings_counts_a_blank_line_inside_the_docstring(tmp_path: Path) -> None:
    """Two short paragraphs measure 3, not 2 — the blank line between them counts,
    which is what makes the ceiling force one contiguous block."""
    file = _write_module(tmp_path, '"""First para.\n\nSecond para.\n"""\n')
    [measurement] = measure_module_docstrings([file], tmp_path)
    assert measurement.lines == 3


def test_measure_module_docstrings_reads_relative_posix_path(tmp_path: Path) -> None:
    nested = tmp_path / "app" / "sub"
    nested.mkdir(parents=True)
    file = _write_module(nested, '"""One line."""\n')
    [measurement] = measure_module_docstrings([file], tmp_path)
    assert measurement.path == "app/sub/m.py"


def test_find_ratchet_violations_reports_every_offender_sorted_by_path() -> None:
    offenders = find_ratchet_violations(
        [ModuleDocstring("app/z.py", 9), ModuleDocstring("app/a.py", 6)], {}
    )
    assert [offender.split()[0] for offender in offenders] == ["app/a.py", "app/z.py"]


def test_find_ratchet_violations_passes_an_exempted_module_over_the_ceiling() -> None:
    over = ModuleDocstring("app/legacy.py", _DOCSTRING_LINE_CEILING + 3)
    assert find_ratchet_violations([over], {"app/legacy.py": "why it earned this"}) == []


def test_find_ratchet_violations_still_flags_a_module_that_is_not_exempted() -> None:
    measurements = [
        ModuleDocstring("app/legacy.py", _DOCSTRING_LINE_CEILING + 3),
        ModuleDocstring("app/other.py", _DOCSTRING_LINE_CEILING + 1),
    ]
    offenders = find_ratchet_violations(measurements, {"app/legacy.py": "why it earned this"})
    assert len(offenders) == 1
    assert "app/other.py" in offenders[0] and "cut it down" in offenders[0]


def test_find_ratchet_violations_flags_a_stale_exception_now_under_the_ceiling() -> None:
    cut = ModuleDocstring("app/legacy.py", _DOCSTRING_LINE_CEILING)
    offenders = find_ratchet_violations([cut], {"app/legacy.py": "why it earned this"})
    assert len(offenders) == 1
    assert "app/legacy.py" in offenders[0] and "delete the stale" in offenders[0]


def test_find_ratchet_violations_flags_a_stale_exception_for_a_missing_module() -> None:
    offenders = find_ratchet_violations([], {"app/gone.py": "why it earned this"})
    assert len(offenders) == 1
    assert "app/gone.py" in offenders[0] and "delete the stale" in offenders[0]


def test_justified_exceptions_ships_empty_and_every_entry_carries_a_reason() -> None:
    """The mechanism lands unused on purpose; if an entry is ever added, its reason
    must be real prose, not an empty string."""
    assert _JUSTIFIED_EXCEPTIONS == {}
    assert all(reason.strip() for reason in _JUSTIFIED_EXCEPTIONS.values())


def test_find_python_files_raises_when_root_has_no_python_files(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="governs no source files"):
        find_python_files(tmp_path)


def test_find_python_files_returns_the_python_files_in_the_root(tmp_path: Path) -> None:
    _write_module(tmp_path, "x = 1\n", name="a.py")
    _write_module(tmp_path, "x = 1\n", name="b.py")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    assert [path.name for path in find_python_files(tmp_path)] == ["a.py", "b.py"]


def test_find_python_files_recurses_into_a_subpackage_but_skips_exempt_parts(tmp_path: Path) -> None:
    """A subpackage added under tests/arch/ must not silently fall out of the rule's
    scope; `__pycache__` (a shared exempt part) must stay out of it."""
    subpackage = tmp_path / "sub"
    subpackage.mkdir()
    _write_module(subpackage, "x = 1\n", name="nested.py")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    _write_module(cache, "x = 1\n", name="stale.py")
    _write_module(tmp_path, "x = 1\n", name="top.py")
    assert [path.name for path in find_python_files(tmp_path)] == ["nested.py", "top.py"]


def test_find_python_files_ignores_a_dot_directory_in_the_scanned_root_prefix(tmp_path: Path) -> None:
    root = tmp_path / ".claude" / "worktrees" / "x" / "arch"
    root.mkdir(parents=True)
    _write_module(root, "x = 1\n")
    assert [path.name for path in find_python_files(root)] == ["m.py"]


def test_find_governed_files_covers_app_and_the_whole_tests_tree() -> None:
    governed = {
        path.relative_to(_REPO_ROOT).as_posix() for path in find_governed_files(_APP_ROOT, _TESTS_ROOT)
    }
    assert "app/main.py" in governed
    assert "tests/conftest.py" in governed
    assert "tests/arch/test_module_docstring_ratchet.py" in governed
    # Every .py under tests/, not just tests/arch/ — the scope this rule governs.
    assert {path for path in governed if path.startswith("tests/") and not path.startswith("tests/arch/")}

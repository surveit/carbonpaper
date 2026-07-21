"""Architecture: a string literal compared at multiple sites must be a named
constant.

The same magic string compared in two places drifts silently when one site
changes; a shared name (Enum or constant) makes the vocabulary explicit. This
was a real review finding: bare run-status strings and a locally re-declared
stage-type set, compared inline instead of through one shared name.

Detection is AST-based: a string literal counts only when it is a direct
operand of ``==``, ``!=``, ``in``, or ``not in`` (a literal passed as a call
argument, assigned to a variable, or used as a dict key is a different
concern, covered by other rules). A literal shorter than two characters is
exempt (an empty string or a single-character literal, e.g. ``","``, is
punctuation, not vocabulary). Occurrences are grouped into "sites": every
comparison inside one function collapses to a single site (repetition inside
one function is a local style call, not a cross-site drift risk), while a
comparison outside any function is its own site per line. A value compared at
two or more distinct sites is a violation.
"""
from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from arch._helpers import parse_module

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"
_EXEMPT_DIR_NAMES = {"tests", "_arch_tests", "__pycache__"}
_COMPARISON_OPS = (ast.Eq, ast.NotEq, ast.In, ast.NotIn)
_MIN_LITERAL_LENGTH = 2

# Pre-existing values flagged by this rule on the real tree. A ratchet: new
# entries are forbidden — a new offender must be named (Enum member or
# module-level constant), not added here. Full sites for each value are in
# the task report; the one exception below is a language idiom, not app
# vocabulary that could drift.
#
# - "__main__": `if __name__ == "__main__":` is Python's own module-entry-
#   point idiom, identical by convention in every script that has one — not
#   an app concept that could drift between two call sites.
_ALLOWLIST: frozenset[str] = frozenset(
    {
        "__main__",
        ".parquet",
        "abs_tol",
        "approved",
        "assistant",
        "error",
        "inf",
        "input_data",
        "json",
        "list[json]",
        "modify",
        "passed",
        "reject",
        "str",
        "text",
    }
)


@dataclass(frozen=True)
class LiteralComparisonSite:
    """One place a string literal is a direct operand of a ``==``/``!=``/
    ``in``/``not in`` comparison.

    ``scope`` identifies the site: the enclosing function
    (``"function:<name>@<def-lineno>"``), so repeated comparisons inside one
    function share a scope and collapse to a single site, or
    ``"module@<lineno>"`` when the comparison sits outside any function, so
    two module-level comparisons on different lines count as two sites.
    """

    value: str
    lineno: int
    scope: str


class _ComparisonLiteralVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.sites: list[LiteralComparisonSite] = []
        self._function_stack: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._function_stack.append(f"function:{node.name}@{node.lineno}")
        self.generic_visit(node)
        self._function_stack.pop()

    def visit_Compare(self, node: ast.Compare) -> None:
        operands = [node.left, *node.comparators]
        scope = self._function_stack[-1] if self._function_stack else f"module@{node.lineno}"
        for op, left, right in zip(node.ops, operands, operands[1:]):
            if not isinstance(op, _COMPARISON_OPS):
                continue
            for operand in (left, right):
                if (
                    isinstance(operand, ast.Constant)
                    and isinstance(operand.value, str)
                    and len(operand.value) >= _MIN_LITERAL_LENGTH
                ):
                    self.sites.append(LiteralComparisonSite(operand.value, operand.lineno, scope))
        self.generic_visit(node)


def find_compared_string_literals(tree: ast.Module) -> list[LiteralComparisonSite]:
    """Every qualifying string-literal comparison site in `tree` — see
    `LiteralComparisonSite` for what counts and what a "site" is."""
    visitor = _ComparisonLiteralVisitor()
    visitor.visit(tree)
    return visitor.sites


def find_source_files(target: Path) -> list[Path]:
    """The .py files under `target` this rule governs: every non-exempt .py
    file below it (skipping tests/, _arch_tests/, and __pycache__)."""
    return sorted(
        path
        for path in target.rglob("*.py")
        if not any(part in _EXEMPT_DIR_NAMES for part in path.relative_to(target).parts)
    )


def find_repeated_literal_values(
    sites_by_file: dict[str, list[LiteralComparisonSite]],
) -> dict[str, list[str]]:
    """value -> sorted "<file>:<lineno> (<scope>)" descriptions, for every
    value compared at two or more distinct (file, scope) sites across
    `sites_by_file` (a same-function repeat collapses to one site, so it
    alone never triggers this)."""
    descriptions_by_value_and_site: dict[str, dict[tuple[str, str], str]] = defaultdict(dict)
    for file, sites in sites_by_file.items():
        for site in sites:
            site_key = (file, site.scope)
            descriptions_by_value_and_site[site.value].setdefault(
                site_key, f"{file}:{site.lineno} ({site.scope})"
            )
    return {
        value: sorted(descriptions.values())
        for value, descriptions in descriptions_by_value_and_site.items()
        if len(descriptions) >= 2
    }


def test_repeated_compared_string_literals_are_named_constants() -> None:
    sites_by_file = {
        path.relative_to(_REPO_ROOT).as_posix(): find_compared_string_literals(parse_module(path))
        for path in find_source_files(_APP_ROOT)
    }
    offenders = {
        value: sites
        for value, sites in find_repeated_literal_values(sites_by_file).items()
        if value not in _ALLOWLIST
    }
    assert not offenders, (
        "a string literal compared at multiple sites drifts silently when "
        "only one site is updated; make it an Enum member or a module-level "
        "named constant and compare via the name:\n  "
        + "\n  ".join(f"{value!r}: {sites}" for value, sites in sorted(offenders.items()))
    )


# --- unit tests for the checker, on inline snippets (red + green) ---------


def test_find_compared_string_literals_flags_equality_comparison() -> None:
    tree = ast.parse('def go(status):\n    return status == "running"\n')
    sites = find_compared_string_literals(tree)
    assert sites == [LiteralComparisonSite("running", 2, "function:go@1")]


def test_find_compared_string_literals_flags_not_equal_comparison() -> None:
    tree = ast.parse('def go(status):\n    return status != "done"\n')
    sites = find_compared_string_literals(tree)
    assert sites == [LiteralComparisonSite("done", 2, "function:go@1")]


def test_find_compared_string_literals_flags_in_comparison_on_left_operand() -> None:
    tree = ast.parse('def go(kinds):\n    return "input" in kinds\n')
    sites = find_compared_string_literals(tree)
    assert sites == [LiteralComparisonSite("input", 2, "function:go@1")]


def test_find_compared_string_literals_flags_not_in_comparison() -> None:
    tree = ast.parse('def go(kinds):\n    return "input" not in kinds\n')
    sites = find_compared_string_literals(tree)
    assert sites == [LiteralComparisonSite("input", 2, "function:go@1")]


def test_find_compared_string_literals_flags_both_sides_of_a_chained_comparison() -> None:
    tree = ast.parse('def go(status):\n    return "aa" == status == "bb"\n')
    sites = find_compared_string_literals(tree)
    assert {site.value for site in sites} == {"aa", "bb"}


def test_find_compared_string_literals_excludes_empty_string() -> None:
    tree = ast.parse('def go(name):\n    return name == ""\n')
    assert find_compared_string_literals(tree) == []


def test_find_compared_string_literals_excludes_single_character_literal() -> None:
    tree = ast.parse('def go(sep):\n    return sep == ","\n')
    assert find_compared_string_literals(tree) == []


def test_find_compared_string_literals_ignores_literal_used_as_call_argument() -> None:
    tree = ast.parse('def go(store):\n    return store.get("running")\n')
    assert find_compared_string_literals(tree) == []


def test_find_compared_string_literals_ignores_literal_used_as_dict_key() -> None:
    tree = ast.parse('def go():\n    return {"running": 1}\n')
    assert find_compared_string_literals(tree) == []


def test_find_compared_string_literals_ignores_literal_assigned_to_a_variable() -> None:
    tree = ast.parse('def go():\n    status = "running"\n    return status\n')
    assert find_compared_string_literals(tree) == []


def test_find_compared_string_literals_uses_module_scope_outside_any_function() -> None:
    tree = ast.parse('_DEFAULT = "running"\nassert _DEFAULT == "running"\n')
    sites = find_compared_string_literals(tree)
    assert sites == [LiteralComparisonSite("running", 2, "module@2")]


def test_find_source_files_excludes_tests_and_arch_tests_dirs(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.py").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "b.py").write_text("")
    (tmp_path / "_arch_tests").mkdir()
    (tmp_path / "_arch_tests" / "c.py").write_text("")
    assert {p.name for p in find_source_files(tmp_path)} == {"a.py"}


def test_find_repeated_literal_values_ignores_repeats_within_one_function() -> None:
    sites_by_file = {
        "a.py": [
            LiteralComparisonSite("running", 2, "function:go@1"),
            LiteralComparisonSite("running", 4, "function:go@1"),
        ]
    }
    assert find_repeated_literal_values(sites_by_file) == {}


def test_find_repeated_literal_values_flags_repeats_across_functions_in_one_file() -> None:
    sites_by_file = {
        "a.py": [
            LiteralComparisonSite("running", 2, "function:go@1"),
            LiteralComparisonSite("running", 8, "function:stop@6"),
        ]
    }
    repeated = find_repeated_literal_values(sites_by_file)
    assert set(repeated) == {"running"}
    assert repeated["running"] == [
        "a.py:2 (function:go@1)",
        "a.py:8 (function:stop@6)",
    ]


def test_find_repeated_literal_values_flags_repeats_across_files() -> None:
    sites_by_file = {
        "a.py": [LiteralComparisonSite("running", 2, "function:go@1")],
        "b.py": [LiteralComparisonSite("running", 3, "function:go@1")],
    }
    assert set(find_repeated_literal_values(sites_by_file)) == {"running"}


def test_find_repeated_literal_values_ignores_a_single_site_value() -> None:
    sites_by_file = {"a.py": [LiteralComparisonSite("running", 2, "function:go@1")]}
    assert find_repeated_literal_values(sites_by_file) == {}

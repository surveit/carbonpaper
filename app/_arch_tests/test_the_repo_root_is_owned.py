"""Architecture: app/core/paths.py is the only place that knows where the checkout is.

A second answer arrives two ways: a module walking up from `__file__` past the `app`
package, or a signature taking a `repo_root` so a distant caller supplies it. Both are
refused, with no allowlist. Anything needing the root calls `app.core.paths.repo_root()`.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from arch import find_governed_files
from arch._helpers import parse_module

_APP = "app"
_OWNER = "app/core/paths.py"
_BANNED_PARAM_NAMES = frozenset({"repo_root", "repo_path", "repo_dir"})


def test_only_the_owner_walks_up_out_of_the_app_package() -> None:
    offenders = [
        f"{_relative(path)}:{lineno}  climbs {levels} level(s) from __file__, out of {_APP}/"
        for path in find_governed_files(__file__)
        if _relative(path) != _OWNER
        for lineno, levels in find_file_ancestor_walks(parse_module(path))
        if levels >= _levels_that_leave_the_app_package(_relative(path))
    ]
    assert not offenders, (
        f"only {_OWNER} may resolve a path above the {_APP}/ package — everywhere else "
        "that is a second answer to 'where is the checkout', free to drift from the "
        f"first. Call app.core.paths.repo_root() instead:\n  " + "\n  ".join(offenders)
    )


def test_no_signature_takes_a_repo_root() -> None:
    offenders = [
        f"{_relative(path)}:{lineno}  def {function}({param}=...)"
        for path in find_governed_files(__file__)
        for lineno, function, param in find_banned_parameter_uses(
            parse_module(path), _BANNED_PARAM_NAMES
        )
    ]
    assert not offenders, (
        "the repo root is a fact, not an argument — a function that needs it calls "
        "app.core.paths.repo_root(). Taking it as a parameter puts the answer in a "
        "caller that has no reason to know it, and nothing then reads it:\n  "
        + "\n  ".join(offenders)
    )


def find_file_ancestor_walks(tree: ast.Module) -> list[tuple[int, int]]:
    """Each `Path(__file__)…` ancestor walk as (line, levels climbed above the file)."""
    inner = _find_links_inside_a_parent_chain(tree)
    return [
        (node.lineno, levels)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Subscript, ast.Attribute))
        and node not in inner
        and (levels := _count_levels_climbed(node)) is not None
    ]


def _find_links_inside_a_parent_chain(tree: ast.Module) -> set[ast.AST]:
    """`a.parent.parent` is one walk of 2, not a walk of 2 and a walk of 1."""
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "parent"
    }


def find_banned_parameter_uses(
    tree: ast.Module, banned_names: frozenset[str]
) -> list[tuple[int, str, str]]:
    offenders: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        if args.vararg is not None:
            params.append(args.vararg)
        if args.kwarg is not None:
            params.append(args.kwarg)
        offenders += [
            (arg.lineno, node.name, arg.arg) for arg in params if arg.arg in banned_names
        ]
    return offenders


# `parents[k]` climbs k+1 levels; a chain of n `.parent`s climbs n. Counting from the
# FILE (not its directory) is what lets one number be compared against path depth.
def _count_levels_climbed(node: ast.AST) -> int | None:
    if isinstance(node, ast.Subscript) and _is_parents_of_a_file_path(node.value):
        index = node.slice
        if isinstance(index, ast.Constant) and isinstance(index.value, int):
            return index.value + 1
        return None
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        levels = 1
        inner: ast.AST = node.value
        while isinstance(inner, ast.Attribute) and inner.attr == "parent":
            levels += 1
            inner = inner.value
        return levels if _is_a_file_path(inner) else None
    return None


def _is_parents_of_a_file_path(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "parents"
        and _is_a_file_path(node.value)
    )


def _is_a_file_path(node: ast.AST) -> bool:
    """`Path(__file__)`, with or without a `.resolve()` in between."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr == "resolve" and _is_a_file_path(node.func.value)
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Path"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "__file__"
    )


# Climbing len(parts) levels from the file lands on the repo root; one less lands on
# `app/`, which a module locating its own templates or data legitimately wants.
def _levels_that_leave_the_app_package(relative_path: str) -> int:
    return len(Path(relative_path).parts)


def _relative(path: Path) -> str:
    return path.relative_to(Path(__file__).resolve().parents[2]).as_posix()


# --- unit tests for the two checkers, on inline snippets (red + green) ----


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("R = Path(__file__).resolve().parents[2]\n", [(1, 3)]),
        ("R = Path(__file__).parents[0]\n", [(1, 1)]),
        ("R = Path(__file__).resolve().parent.parent\n", [(1, 2)]),
        ("R = Path(__file__).parent\n", [(1, 1)]),
    ],
)
def test_find_file_ancestor_walks_counts_levels(source: str, expected: list[tuple[int, int]]) -> None:
    assert find_file_ancestor_walks(ast.parse(source)) == expected


def test_find_file_ancestor_walks_ignores_a_walk_from_another_path() -> None:
    assert find_file_ancestor_walks(ast.parse("R = Path(target).resolve().parents[2]\n")) == []


def test_find_file_ancestor_walks_ignores_a_join_that_climbs_nothing() -> None:
    assert find_file_ancestor_walks(ast.parse('R = Path(__file__).with_suffix(".txt")\n')) == []


def test_the_owner_is_flagged_by_the_walk_checker_when_it_is_not_exempt() -> None:
    """Proof the rule has teeth: the one sanctioned walk fails the predicate it is exempt from."""
    owner = Path(__file__).resolve().parents[2] / _OWNER
    walks = find_file_ancestor_walks(parse_module(owner))
    assert any(levels >= _levels_that_leave_the_app_package(_OWNER) for _, levels in walks)


def test_the_boundary_sits_between_the_app_dir_and_the_repo_root() -> None:
    boundary = _levels_that_leave_the_app_package("app/web/config.py")
    # The two lines app/web/config.py held side by side: APP_DIR stays, the
    # second repo root beside it is what this rule deleted.
    (_, app_dir), = find_file_ancestor_walks(
        ast.parse("APP_DIR = Path(__file__).resolve().parent.parent\n"))
    (_, repo) = find_file_ancestor_walks(
        ast.parse("REPO_ROOT = Path(__file__).resolve().parent.parent.parent\n"))[0]
    assert app_dir < boundary <= repo


def test_find_banned_parameter_uses_flags_a_keyword_only_repo_root() -> None:
    tree = ast.parse("def go(*, repo_root):\n    return 1\n")
    assert find_banned_parameter_uses(tree, _BANNED_PARAM_NAMES) == [(1, "go", "repo_root")]


def test_find_banned_parameter_uses_ignores_a_local_named_repo_root() -> None:
    tree = ast.parse("def go(ctx):\n    repo_root = ctx.root\n    return repo_root\n")
    assert find_banned_parameter_uses(tree, _BANNED_PARAM_NAMES) == []


def test_find_banned_parameter_uses_ignores_a_call_to_the_owner() -> None:
    tree = ast.parse("def go():\n    return repo_root() / 'x'\n")
    assert find_banned_parameter_uses(tree, _BANNED_PARAM_NAMES) == []


@pytest.mark.parametrize("banned_name", sorted(_BANNED_PARAM_NAMES))
def test_each_banned_name_is_flagged_individually(banned_name: str) -> None:
    tree = ast.parse(f"def go({banned_name}):\n    return 1\n")
    assert find_banned_parameter_uses(tree, _BANNED_PARAM_NAMES) == [(1, "go", banned_name)]

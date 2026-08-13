"""Architecture: runtime signatures take objects, not project directories.

Detection is AST-based over every named parameter of every def/method
(positional-only, ordinary, *args, keyword-only, **kwargs). Lambdas are
skipped.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from arch import find_governed_files
from arch._helpers import parse_module

_BANNED_PARAM_NAMES = frozenset({"project_dir", "project_path", "project_root"})


@dataclass(frozen=True)
class RuntimeObjectRule:
    scope_file: Path
    banned_names: frozenset[str]
    rationale: str
    # Empty, and it stays empty: a new offender is fixed, never listed.
    allowlist: frozenset[tuple[str, str, str]] = field(default_factory=frozenset)


_RULE = RuntimeObjectRule(
    scope_file=Path(__file__),
    banned_names=_BANNED_PARAM_NAMES,
    rationale=(
        "the runtime operates on in-memory objects (Workflow, stages, "
        "frames); reading project directories is the services layer's job"
    ),
)


def find_function_parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    args = node.args
    params: list[ast.arg] = [*args.posonlyargs, *args.args]
    if args.vararg is not None:
        params.append(args.vararg)
    params.extend(args.kwonlyargs)
    if args.kwarg is not None:
        params.append(args.kwarg)
    return params


def find_banned_parameter_uses(
    tree: ast.Module, banned_names: frozenset[str]
) -> list[tuple[int, str, str]]:
    offenders: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for arg in find_function_parameters(node):
            if arg.arg in banned_names:
                offenders.append((arg.lineno, node.name, arg.arg))
    return offenders


def test_runtime_signatures_take_objects_not_project_dirs() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    offenders = [
        (path.relative_to(repo_root).as_posix(), function_name, param_name)
        for path in find_governed_files(str(_RULE.scope_file))
        for _, function_name, param_name in find_banned_parameter_uses(
            parse_module(path), _RULE.banned_names
        )
        if (path.relative_to(repo_root).as_posix(), function_name, param_name) not in _RULE.allowlist
    ]
    assert not offenders, (
        f"{_RULE.rationale}:\n  "
        + "\n  ".join(f"{file}  def {fn}({param}=...)" for file, fn, param in offenders)
    )


# --- unit tests for the checker, on inline snippets (red + green) ---------


def test_find_function_parameters_covers_positional_and_ordinary_args() -> None:
    tree = ast.parse("def go(a, b, /, c):\n    return 1\n")
    (node,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    assert [p.arg for p in find_function_parameters(node)] == ["a", "b", "c"]


def test_find_function_parameters_covers_star_args_and_kwonly_and_kwargs() -> None:
    tree = ast.parse("def go(a, *args, b, **kwargs):\n    return 1\n")
    (node,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    assert [p.arg for p in find_function_parameters(node)] == ["a", "args", "b", "kwargs"]


def test_find_banned_parameter_uses_flags_def_parameter() -> None:
    tree = ast.parse("def load(project_dir):\n    return 1\n")
    assert find_banned_parameter_uses(tree, _BANNED_PARAM_NAMES) == [(1, "load", "project_dir")]


def test_find_banned_parameter_uses_flags_async_def_parameter() -> None:
    tree = ast.parse("async def load(project_root):\n    return 1\n")
    assert find_banned_parameter_uses(tree, _BANNED_PARAM_NAMES) == [(1, "load", "project_root")]


def test_find_banned_parameter_uses_flags_method_parameter() -> None:
    tree = ast.parse(
        "class Loader:\n"
        "    def load(self, project_path):\n"
        "        return 1\n"
    )
    assert find_banned_parameter_uses(tree, _BANNED_PARAM_NAMES) == [(2, "load", "project_path")]


def test_find_banned_parameter_uses_flags_keyword_only_parameter() -> None:
    tree = ast.parse("def load(*, project_dir):\n    return 1\n")
    assert find_banned_parameter_uses(tree, _BANNED_PARAM_NAMES) == [(1, "load", "project_dir")]


def test_find_banned_parameter_uses_flags_positional_only_parameter() -> None:
    tree = ast.parse("def load(project_dir, /):\n    return 1\n")
    assert find_banned_parameter_uses(tree, _BANNED_PARAM_NAMES) == [(1, "load", "project_dir")]


def test_find_banned_parameter_uses_skips_lambda_parameters() -> None:
    tree = ast.parse("go = lambda project_dir: project_dir\n")
    assert find_banned_parameter_uses(tree, _BANNED_PARAM_NAMES) == []


def test_find_banned_parameter_uses_ignores_similarly_named_local_variable() -> None:
    tree = ast.parse("def load(ctx):\n    project_dir = ctx['project_dir']\n    return project_dir\n")
    assert find_banned_parameter_uses(tree, _BANNED_PARAM_NAMES) == []


def test_find_banned_parameter_uses_matches_exact_name_not_substring() -> None:
    tree = ast.parse("def load(repo_root):\n    return 1\n")
    assert find_banned_parameter_uses(tree, _BANNED_PARAM_NAMES) == []


def test_find_banned_parameter_uses_ignores_clean_snippet() -> None:
    tree = ast.parse("def load(workflow, stage):\n    return workflow, stage\n")
    assert find_banned_parameter_uses(tree, _BANNED_PARAM_NAMES) == []


@pytest.mark.parametrize("banned_name", sorted(_BANNED_PARAM_NAMES))
def test_each_banned_name_is_flagged_individually(banned_name: str) -> None:
    tree = ast.parse(f"def load({banned_name}):\n    return 1\n")
    assert find_banned_parameter_uses(tree, _BANNED_PARAM_NAMES) == [(1, "load", banned_name)]

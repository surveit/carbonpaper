"""Architecture: a project is referenced by its id, and that id is called `project_id`.

A project's NAME is a label that repeats and changes; only the id addresses it. So the
bare word `project` is reserved for a `Project`, and a directory is nobody's parameter —
`resolve_project_dir` is called by the modules listed below and by nothing else.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from arch import find_governed_files
from arch._helpers import find_imported_modules, parse_module

_BANNED_NAMES = frozenset({"project_dir", "pdir", "project_directory", "project_path", "project_root"})
_RESOLVER = "resolve_project_dir"

# Every module that may turn a project id into a path, and the file it owns there.
# A module joins onto what it resolves and keeps the result local; nothing hands a
# project directory to anything else.
_PATH_OWNERS: dict[str, str] = {
    "app/services/workspace.py": "the resolver itself, plus runs/ and schemas/",
    "app/services/project.py": "project.json, and the working copy's own directory",
    "app/evals/store.py": "eval_data/ uploads, eval_run/ output and its result table",
    "app/evals/runner.py": "the eval run's result_ref, recorded project-relative",
    "app/services/review_packet/data.py": "relocating a run input whose recorded absolute path went stale",
}


def test_a_string_that_addresses_a_project_is_called_project_id() -> None:
    """`project` is a loaded word: as a `str` it reads as the name, which is a different thing."""
    offenders = [
        f"{_relative(path)}:{lineno}  def {function}(project: {annotation})"
        for path in find_governed_files(__file__)
        for lineno, function, annotation in find_bare_project_parameters(parse_module(path))
    ]
    assert not offenders, (
        "a parameter holding a project's id is `project_id: str`. Bare `project` is "
        "reserved for a `Project` — as a `str` it reads as the project's NAME, which is a "
        "label that repeats and changes and addresses nothing:\n  " + "\n  ".join(offenders)
    )


def test_nothing_names_a_project_by_its_directory() -> None:
    offenders = [
        f"{_relative(path)}:{lineno}  {detail}"
        for path in find_governed_files(__file__)
        for lineno, detail in find_project_directory_names(parse_module(path))
    ]
    assert not offenders, (
        "a project is addressed by its id (`project_id: str`). A parameter or variable holding its "
        "DIRECTORY states an invariant nothing checks — that the path and the id agree — "
        "and hands a filesystem to code that wanted a name. Take the id and let the one "
        f"module that owns the file call {_RESOLVER}:\n  " + "\n  ".join(offenders)
    )


def test_only_a_path_owner_resolves_a_project_directory() -> None:
    offenders = [
        _relative(path)
        for path in find_governed_files(__file__)
        if _relative(path) not in _PATH_OWNERS
        and "app.services.workspace" in find_imported_modules(parse_module(path))
        and _RESOLVER in _find_imported_names(parse_module(path))
    ]
    assert not offenders, (
        f"{_RESOLVER} answers 'where does this project's files live', which only a module "
        "that reads or writes one of those files has any reason to ask. Everywhere else, "
        "take the id and call a service that owns the file:\n  " + "\n  ".join(offenders)
    )


def test_every_listed_path_owner_still_resolves_something() -> None:
    """The list shrinks as storage moves into the document store; a stale row hides that."""
    stale = [
        module for module in _PATH_OWNERS
        if _RESOLVER not in (_REPO_ROOT / module).read_text(encoding="utf-8")
    ]
    assert not stale, (
        f"these modules no longer call {_RESOLVER} — delete their row from _PATH_OWNERS "
        "so the list keeps naming exactly what is still on disk:\n  " + "\n  ".join(stale)
    )


def find_project_directory_names(tree: ast.Module) -> list[tuple[int, str]]:
    """A named parameter, or a local bound to one — the two ways a directory travels."""
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.arg in _BANNED_NAMES:
            offenders.append((node.lineno, f"parameter `{node.arg}` — take a project id"))
        elif (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Store)
            and node.id in _BANNED_NAMES
        ):
            offenders.append((node.lineno, f"binds `{node.id}` — resolve it where it is read"))
    return sorted(offenders)


def find_bare_project_parameters(tree: ast.Module) -> list[tuple[int, str, str]]:
    """A parameter named `project` that is not annotated `Project`. Fields are not parameters."""
    offenders: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            if arg.arg != "project":
                continue
            annotation = ast.unparse(arg.annotation) if arg.annotation else "?"
            if annotation != "Project":
                offenders.append((arg.lineno, node.name, annotation))
    return offenders


def _find_imported_names(tree: ast.Module) -> set[str]:
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _relative(path: Path) -> str:
    return path.relative_to(_REPO_ROOT).as_posix()


# --- unit tests for the checker, on inline snippets (red + green) ---------


@pytest.mark.parametrize("banned_name", sorted(_BANNED_NAMES))
def test_each_banned_name_is_flagged_as_a_parameter(banned_name: str) -> None:
    tree = ast.parse(f"def load({banned_name}):\n    return 1\n")
    assert find_project_directory_names(tree) == [
        (1, f"parameter `{banned_name}` — take a project id")
    ]


@pytest.mark.parametrize("banned_name", sorted(_BANNED_NAMES))
def test_each_banned_name_is_flagged_as_a_local(banned_name: str) -> None:
    tree = ast.parse(f"def load(project):\n    {banned_name} = resolve(project)\n    return 1\n")
    assert find_project_directory_names(tree) == [
        (2, f"binds `{banned_name}` — resolve it where it is read")
    ]


def test_a_keyword_only_parameter_is_flagged() -> None:
    tree = ast.parse("def load(*, project_dir):\n    return 1\n")
    assert find_project_directory_names(tree) == [(1, "parameter `project_dir` — take a project id")]


def test_reading_the_resolver_inline_is_not_a_binding() -> None:
    """The sanctioned shape: resolve where it is read, keep no name for it."""
    tree = ast.parse("def load(project):\n    return (resolve_project_dir(project) / 'x').read_text()\n")
    assert find_project_directory_names(tree) == []


def test_a_project_id_parameter_is_not_flagged() -> None:
    tree = ast.parse("def load(project: str, project_id: str):\n    return 1\n")
    assert find_project_directory_names(tree) == []


def test_a_bare_project_string_parameter_is_flagged() -> None:
    tree = ast.parse("def load(project: str):\n    return 1\n")
    assert find_bare_project_parameters(tree) == [(1, "load", "str")]


def test_a_project_model_parameter_is_not_flagged() -> None:
    """The one correct use of the bare word: it IS a Project."""
    tree = ast.parse("def label(project: Project):\n    return project.name\n")
    assert find_bare_project_parameters(tree) == []


def test_a_project_id_parameter_is_not_flagged_by_the_naming_rule() -> None:
    tree = ast.parse("def load(project_id: str):\n    return 1\n")
    assert find_bare_project_parameters(tree) == []


def test_a_model_field_named_project_is_not_a_parameter() -> None:
    """Renaming a stored field is a migration; this rule governs signatures only."""
    tree = ast.parse("class RunManifest(PersistedModel):\n    project: str\n")
    assert find_bare_project_parameters(tree) == []


def test_find_imported_names_sees_a_from_import() -> None:
    tree = ast.parse("from app.services.workspace import resolve_project_dir, projects_dir\n")
    assert _find_imported_names(tree) == {"resolve_project_dir", "projects_dir"}

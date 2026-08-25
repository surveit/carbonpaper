"""Architecture: one function lists projects, and it is the only place `private` is applied."""
from __future__ import annotations

import ast
from pathlib import Path

from arch import scan_all_source
from arch._helpers import parse_module

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SERVICE = "app/services/project.py"


def test_only_the_project_service_enumerates_project_records() -> None:
    offenders = [
        f"{_relative(path)}:{lineno}"
        for path in _governed_files()
        if _relative(path) != _SERVICE
        for lineno in find_record_enumerations(parse_module(path))
    ]
    assert not offenders, (
        "Project.list() hands back every stored project, private ones included. There is no "
        "listing that shows a private project — /admin is served without auth, so an "
        "operator-only view would be a public one — and this is how that stays true: "
        f"{_SERVICE}'s list_project_listings walks the working copies and drops any whose "
        "record is private, and list_projects wraps it. Call one of those, and a project "
        "deleted out of the workspace drops out for free:\n  " + "\n  ".join(offenders)
    )


def test_the_service_this_rule_names_still_exists() -> None:
    """A rename would leave the rule governing every file and protecting nothing."""
    assert (_REPO_ROOT / _SERVICE).is_file(), f"stale path in this rule: {_SERVICE}"


def find_record_enumerations(tree: ast.Module) -> list[int]:
    """`Project.list()` — the one call that reads every stored project."""
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "list"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "Project"
    )


def _governed_files() -> list[Path]:
    return [path for path in scan_all_source() if _relative(path).startswith("app/")]


def _relative(path: Path) -> str:
    return path.relative_to(_REPO_ROOT).as_posix()


# --- unit tests for the checker, on inline snippets (red + green) -------------


def test_the_checker_sees_the_enumeration() -> None:
    assert find_record_enumerations(ast.parse("rows = Project.list()\n")) == [1]


def test_the_checker_sees_it_inside_a_comprehension() -> None:
    assert find_record_enumerations(ast.parse("ids = [r.id for r in Project.list()]\n")) == [1]


def test_the_checker_ignores_a_single_record_load() -> None:
    assert find_record_enumerations(ast.parse("r = Project.load_or_none(pid)\n")) == []


def test_the_checker_ignores_another_records_listing() -> None:
    assert find_record_enumerations(ast.parse("rows = AgentSession.list()\n")) == []

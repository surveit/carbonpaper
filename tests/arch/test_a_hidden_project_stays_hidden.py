"""Architecture: a project the reader may not see is dropped in one place, not at each list.

Two things hide one: `private` on its record, and a working copy `delete_project` removed
while keeping the record. Both are applied by `app.services.project`, so no listing surface
can forget them and only the admin screens name the one function that shows a private project.
"""
from __future__ import annotations

import ast
from pathlib import Path

from arch import scan_all_source
from arch._helpers import parse_module

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SERVICE = "app/services/project.py"

# Scans directories, so a private record is invisible to it. Its caller subtracts.
_DIRECTORY_SCAN = "find_workspace_project_ids"
_SUBTRACTION = "find_private_project_ids"
_DIRECTORY_SCAN_CALLERS = frozenset({_SERVICE, "app/web/loading.py"})

_ESCAPE_HATCH = "list_project_listings_including_private"
_ESCAPE_HATCH_CALLERS = frozenset({
    _SERVICE,
    # The operator's own screens. They say `private` on the row, so the flag is
    # readable exactly where the project it hides is still listed.
    "app/web/admin/workspace_router.py",
    "app/web/admin/cache_router.py",
})


def test_only_the_project_service_enumerates_project_records() -> None:
    offenders = [
        f"{_relative(path)}:{lineno}"
        for path in _governed_files()
        if _relative(path) != _SERVICE
        for lineno in find_record_enumerations(parse_module(path))
    ]
    assert not offenders, (
        "Project.list() hands back every stored project, including one that is private and "
        "one whose working copy delete_project removed. Call app.services.project's "
        "list_project_listings / list_projects, which drop both:\n  " + "\n  ".join(offenders)
    )


def test_a_directory_scan_subtracts_the_private_projects() -> None:
    for path in _governed_files():
        relative = _relative(path)
        names = find_imported_names(parse_module(path))
        if _DIRECTORY_SCAN not in names:
            continue
        assert relative in _DIRECTORY_SCAN_CALLERS, (
            f"{relative} lists project DIRECTORIES. `private` is on the record, so a "
            f"directory scan shows a private project. Use list_project_listings instead."
        )
        assert relative == _SERVICE or _SUBTRACTION in names, (
            f"{relative} scans project directories without importing {_SUBTRACTION}, so a "
            f"private project is listed there. Subtract them, as app/web/loading.py does."
        )


def test_only_the_admin_screens_list_a_private_project() -> None:
    offenders = [
        relative
        for path in _governed_files()
        if (relative := _relative(path)) not in _ESCAPE_HATCH_CALLERS
        and _ESCAPE_HATCH in find_imported_names(parse_module(path))
    ]
    assert not offenders, (
        f"{_ESCAPE_HATCH} is the one listing a private project appears on, and the admin "
        "screens are where it appears because they say so on the row. Everywhere else "
        "calls list_project_listings:\n  " + "\n  ".join(offenders)
    )


def test_every_named_caller_still_exists() -> None:
    """A renamed module would silently widen all three rules to nothing."""
    named = {_SERVICE, *_DIRECTORY_SCAN_CALLERS, *_ESCAPE_HATCH_CALLERS}
    missing = sorted(rel for rel in named if not (_REPO_ROOT / rel).is_file())
    assert not missing, f"stale entry in this rule: {missing}"


def find_record_enumerations(tree: ast.Module) -> list[int]:
    """`Project.list()` — the call that reads every stored project, however it is aliased."""
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "list"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "Project"
    )


def find_imported_names(tree: ast.Module) -> set[str]:
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }


def _governed_files() -> list[Path]:
    return [path for path in scan_all_source()
            if _relative(path).startswith("app/")]


def _relative(path: Path) -> str:
    return path.relative_to(_REPO_ROOT).as_posix()


# --- unit tests for the checkers, on inline snippets (red + green) ------------


def test_the_enumeration_checker_sees_the_call() -> None:
    assert find_record_enumerations(ast.parse("rows = Project.list()\n")) == [1]


def test_the_enumeration_checker_ignores_a_single_record_load() -> None:
    assert find_record_enumerations(ast.parse("r = Project.load_or_none(pid)\n")) == []


def test_the_enumeration_checker_ignores_another_records_listing() -> None:
    assert find_record_enumerations(ast.parse("rows = AgentSession.list()\n")) == []


def test_the_import_checker_reads_a_from_import() -> None:
    tree = ast.parse("from app.services.project import find_workspace_project_ids, has_document\n")
    assert find_imported_names(tree) == {"find_workspace_project_ids", "has_document"}

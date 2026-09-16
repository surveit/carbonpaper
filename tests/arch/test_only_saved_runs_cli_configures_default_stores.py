from __future__ import annotations

import ast
from pathlib import Path

import pytest

from arch._helpers import find_relative_import_targets, parse_module
from arch.scope import find_source_files_under

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EVALS_ROOT = _REPO_ROOT / "evals"
_SAVED_RUNS_CLI = "evals/runs/cli.py"
_SAVED_RUNS_CLI_MODULE = "evals.runs.cli"
_SAVED_RUNS_ENTRYPOINT = "evals/runs/__main__.py"
# app.seeds.bootstrap re-exports store_config's document-store configurer.
_CONFIGURER_MODULES = ("app.core.store_config", "app.seeds.bootstrap")
_COMPOSITION_ROOTS = ("app.main", "app.cli", "app.seeds.__main__")
# By name too: once anything has loaded a module, a qualified call into it needs no import.
_CONFIGURERS = (
    "configure_default_stores",
    "configure_default_document_store",
    "configure_projects_dir_from_env",
)


def test_only_the_saved_runs_cli_points_the_app_at_the_real_stores() -> None:
    offenders = find_real_store_reaches(find_source_files_under(_EVALS_ROOT), _REPO_ROOT)
    assert not offenders, (
        f"within evals/, only {_SAVED_RUNS_CLI} may import {', '.join(_CONFIGURER_MODULES)} or a "
        f"composition root ({', '.join(_COMPOSITION_ROOTS)}), or name a configurer "
        f"({', '.join(_CONFIGURERS)}) — everything else works in a throwaway workspace, never "
        "the machine's real stores:\n  " + "\n  ".join(offenders)
    )


def test_under_evals_only_the_entrypoint_imports_the_saved_runs_cli() -> None:
    offenders = find_saved_runs_cli_imports(find_source_files_under(_EVALS_ROOT), _REPO_ROOT)
    assert not offenders, (
        f"{_SAVED_RUNS_CLI} is the one file under evals/ allowed to point the app at the "
        f"machine's real stores, so only {_SAVED_RUNS_ENTRYPOINT} may import it — every other "
        "importer would reach those stores through it:\n  " + "\n  ".join(offenders)
    )


def find_real_store_reaches(paths: list[Path], repo_root: Path) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        relative = path.relative_to(repo_root).as_posix()
        if relative == _SAVED_RUNS_CLI:
            continue
        offenders += [
            f"{relative}:{lineno}  {reached}"
            for lineno, reached in find_real_store_reaches_in(parse_module(path))
        ]
    return offenders


def find_real_store_reaches_in(tree: ast.Module) -> list[tuple[int, str]]:
    return sorted(
        reach
        for node in ast.walk(tree)
        for reach in [*_find_protected_imports(node), *_find_configurer_names(node)]
    )


def find_saved_runs_cli_imports(paths: list[Path], repo_root: Path) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        relative = path.relative_to(repo_root)
        if relative.as_posix() == _SAVED_RUNS_ENTRYPOINT:
            continue
        offenders += [
            f"{relative.as_posix()}:{lineno}  {imported}"
            for lineno, imported in find_saved_runs_cli_imports_in(
                parse_module(path), relative.parent.parts
            )
        ]
    return offenders


def find_saved_runs_cli_imports_in(
    tree: ast.Module, package: tuple[str, ...]
) -> list[tuple[int, str]]:
    return sorted({
        (node.lineno, imported)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for imported in _find_imported_paths_inside_evals(node, package)
        if imported == _SAVED_RUNS_CLI_MODULE
    })


def _find_protected_imports(node: ast.AST) -> list[tuple[int, str]]:
    if not isinstance(node, (ast.Import, ast.ImportFrom)):
        return []
    imported = _find_imported_paths(node)
    return [
        (node.lineno, module)
        for module in (*_CONFIGURER_MODULES, *_COMPOSITION_ROOTS)
        if any(path == module or path.startswith(f"{module}.") for path in imported)
    ]


def _find_imported_paths(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level or node.module is None:
        return []  # relative: resolves inside evals/, never to app/
    return [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]


def _find_imported_paths_inside_evals(
    node: ast.Import | ast.ImportFrom, package: tuple[str, ...]
) -> list[str]:
    if not isinstance(node, ast.ImportFrom) or not node.level:
        return _find_imported_paths(node)
    resolved = find_relative_import_targets(ast.Module(body=[node], type_ignores=[]), package)
    return [*resolved, *(f"{target}.{alias.name}" for target in resolved for alias in node.names)]


def _find_configurer_names(node: ast.AST) -> list[tuple[int, str]]:
    # Every reference, not only calls: a configurer handed on uncalled still gets called.
    if isinstance(node, ast.ImportFrom):
        names = [alias.name for alias in node.names]
    elif isinstance(node, ast.Name):
        names = [node.id]
    elif isinstance(node, ast.Attribute):
        names = [node.attr]
    else:
        return []
    return [(node.lineno, name) for name in names if name in _CONFIGURERS]


# --- unit tests for the checker, on inline snippets (red + green) ---------


def test_find_real_store_reaches_in_flags_every_import_form_of_a_configurer_module() -> None:
    tree = ast.parse(
        "import app.core.store_config\n"
        "import app.core.store_config as config\n"
        "from app.core.store_config import configure_default_document_store\n"
        "from app.core import files, store_config\n"
        "from app.seeds.bootstrap import configure_default_document_store\n"
        "from app.seeds import bootstrap as seed_bootstrap\n"
    )
    assert find_real_store_reaches_in(tree) == [
        (1, "app.core.store_config"),
        (2, "app.core.store_config"),
        (3, "app.core.store_config"),
        (3, "configure_default_document_store"),
        (4, "app.core.store_config"),
        (5, "app.seeds.bootstrap"),
        (5, "configure_default_document_store"),
        (6, "app.seeds.bootstrap"),
    ]


def test_find_real_store_reaches_in_flags_an_import_of_each_composition_root() -> None:
    tree = ast.parse(
        "import app.main\n"
        "from app import cli\n"
        "from app.main import app\n"
        "from app.seeds.__main__ import main\n"
    )
    assert find_real_store_reaches_in(tree) == [
        (1, "app.main"),
        (2, "app.cli"),
        (3, "app.main"),
        (4, "app.seeds.__main__"),
    ]


def test_find_real_store_reaches_in_flags_any_use_of_the_projects_dir_configurer() -> None:
    tree = ast.parse(
        "from app.web.config import configure_projects_dir_from_env\n"
        "from app.services import workspace\n"
        "configure_projects_dir_from_env()\n"
        "hooks = [workspace.configure_projects_dir_from_env]\n"
    )
    assert find_real_store_reaches_in(tree) == [
        (1, "configure_projects_dir_from_env"),
        (3, "configure_projects_dir_from_env"),
        (4, "configure_projects_dir_from_env"),
    ]


def test_find_real_store_reaches_in_flags_a_qualified_call_into_an_already_loaded_module() -> None:
    tree = ast.parse("import app.core.files\napp.core.store_config.configure_default_stores()\n")
    assert find_real_store_reaches_in(tree) == [(2, "configure_default_stores")]


def test_find_real_store_reaches_in_flags_a_call_through_an_aliased_parent_package() -> None:
    tree = ast.parse("import app.core as core\ncore.store_config.configure_default_document_store()\n")
    assert find_real_store_reaches_in(tree) == [(2, "configure_default_document_store")]


def test_find_real_store_reaches_in_ignores_throwaway_configurers_look_alikes_and_prose() -> None:
    tree = ast.parse(
        '"""Never imports app.core.store_config or calls configure_projects_dir_from_env."""\n'
        "import app\n"
        "import app.clinic\n"
        "from app.core import files\n"
        "from app.core.persistence import configure_store\n"
        "from app.maintenance import schedule\n"
        "from app.services import workspace\n"
        "from . import recipe\n"
        "configure_store(store)\n"
        "workspace.set_projects_dir(root)\n"
    )
    assert find_real_store_reaches_in(tree) == []


def test_find_real_store_reaches_exempts_the_saved_runs_cli_by_its_path(tmp_path: Path) -> None:
    for relative in ("evals/runs/cli.py", "evals/harness/cli.py", "evals/runs/_probe.py"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "from app.core import store_config\nstore_config.configure_default_document_store()\n",
            encoding="utf-8",
        )
    assert find_real_store_reaches(sorted(tmp_path.glob("evals/*/*.py")), tmp_path) == [
        "evals/harness/cli.py:1  app.core.store_config",
        "evals/harness/cli.py:2  configure_default_document_store",
        "evals/runs/_probe.py:1  app.core.store_config",
        "evals/runs/_probe.py:2  configure_default_document_store",
    ]


def test_find_saved_runs_cli_imports_in_flags_every_absolute_import_form() -> None:
    tree = ast.parse(
        "import evals.runs.cli\n"
        "import evals.runs.cli as saved_runs\n"
        "from evals.runs.cli import main\n"
        "from evals.runs import cli\n"
    )
    assert find_saved_runs_cli_imports_in(tree, ("evals", "runs")) == [
        (1, "evals.runs.cli"),
        (2, "evals.runs.cli"),
        (3, "evals.runs.cli"),
        (4, "evals.runs.cli"),
    ]


@pytest.mark.parametrize(
    ("source", "package"),
    [
        ("from . import cli\nfrom .cli import main\n", ("evals", "runs")),
        ("from ..runs import cli\nfrom ..runs.cli import main\n", ("evals", "harness")),
    ],
    ids=["inside the runs package", "from a sibling package"],
)
def test_find_saved_runs_cli_imports_in_resolves_a_relative_import_against_its_package(
    source: str, package: tuple[str, ...]
) -> None:
    assert find_saved_runs_cli_imports_in(ast.parse(source), package) == [
        (1, "evals.runs.cli"),
        (2, "evals.runs.cli"),
    ]


def test_find_saved_runs_cli_imports_in_ignores_the_other_modules_of_the_runs_package() -> None:
    tree = ast.parse(
        "from evals.runs.capture import capture_run\n"
        "from evals.runs import rebuild, recipe\n"
        "from . import workspace\n"
        "from .rebuild import rebuild_run\n"
        "import evals.harness\n"
    )
    assert find_saved_runs_cli_imports_in(tree, ("evals", "runs")) == []


def test_find_saved_runs_cli_imports_exempts_the_entrypoint_by_its_path(tmp_path: Path) -> None:
    for relative in (_SAVED_RUNS_ENTRYPOINT, "evals/runs/probe.py", "evals/harness/__main__.py"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("from evals.runs.cli import main\n", encoding="utf-8")
    assert find_saved_runs_cli_imports(sorted(tmp_path.glob("evals/*/*.py")), tmp_path) == [
        "evals/harness/__main__.py:1  evals.runs.cli",
        "evals/runs/probe.py:1  evals.runs.cli",
    ]

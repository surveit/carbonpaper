from __future__ import annotations

import ast
from pathlib import Path

from arch._helpers import parse_module
from arch.scope import find_source_files_under

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EVALS_ROOT = _REPO_ROOT / "evals"
_SAVED_RUNS_CLI = "evals/runs/cli.py"
_REAL_STORE_CONFIGURERS = frozenset({"configure_default_stores", "configure_projects_dir_from_env"})


def test_only_the_saved_runs_cli_points_the_app_at_the_real_stores() -> None:
    offenders = find_real_store_configurer_uses(find_source_files_under(_EVALS_ROOT), _REPO_ROOT)
    assert not offenders, (
        f"within evals/, only {_SAVED_RUNS_CLI} may import, call or refer to "
        "configure_default_stores or configure_projects_dir_from_env — everything else works "
        "in a throwaway workspace, never the machine's real stores:\n  " + "\n  ".join(offenders)
    )


def find_real_store_configurer_uses(paths: list[Path], repo_root: Path) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        relative = path.relative_to(repo_root).as_posix()
        if relative == _SAVED_RUNS_CLI:
            continue
        offenders += [
            f"{relative}:{lineno}  {name}" for lineno, name in find_configurer_names(parse_module(path))
        ]
    return offenders


def find_configurer_names(tree: ast.Module) -> list[tuple[int, str]]:
    return sorted(
        (lineno, name)
        for node in ast.walk(tree)
        for lineno, name in _find_names_in(node)
        if name in _REAL_STORE_CONFIGURERS
    )


def _find_names_in(node: ast.AST) -> list[tuple[int, str]]:
    # Every reference, not only calls: a configurer handed on uncalled still gets called.
    if isinstance(node, ast.ImportFrom):
        return [(node.lineno, alias.name) for alias in node.names]
    if isinstance(node, ast.Name):
        return [(node.lineno, node.id)]
    if isinstance(node, ast.Attribute):
        return [(node.lineno, node.attr)]
    return []


# --- unit tests for the checker, on inline snippets (red + green) ---------


def test_find_configurer_names_flags_an_import_and_a_bare_call() -> None:
    tree = ast.parse(
        "from app.core.store_config import configure_default_stores\n"
        "from app.services.workspace import configure_projects_dir_from_env as configure\n"
        "configure_default_stores()\n"
    )
    assert find_configurer_names(tree) == [
        (1, "configure_default_stores"),
        (2, "configure_projects_dir_from_env"),
        (3, "configure_default_stores"),
    ]


def test_find_configurer_names_flags_a_call_reached_through_its_module() -> None:
    tree = ast.parse(
        "from app.core import store_config\n"
        "import app.services.workspace\n"
        "store_config.configure_default_stores()\n"
        "app.services.workspace.configure_projects_dir_from_env()\n"
    )
    assert find_configurer_names(tree) == [
        (3, "configure_default_stores"),
        (4, "configure_projects_dir_from_env"),
    ]


def test_find_configurer_names_ignores_the_throwaway_configurers_and_a_mention_in_prose() -> None:
    tree = ast.parse(
        '"""Never calls configure_default_stores."""\n'
        "from app.core.persistence import configure_store\n"
        "from app.services import workspace\n"
        "configure_store(store)\n"
        "workspace.set_projects_dir(root)\n"
    )
    assert find_configurer_names(tree) == []


def test_find_real_store_configurer_uses_exempts_the_saved_runs_cli_by_its_path(tmp_path: Path) -> None:
    for relative in ("evals/runs/cli.py", "evals/harness/cli.py", "evals/runs/_probe.py"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("from app.core import store_config\nstore_config.configure_default_stores()\n", encoding="utf-8")
    assert find_real_store_configurer_uses(sorted(tmp_path.glob("evals/*/*.py")), tmp_path) == [
        "evals/harness/cli.py:2  configure_default_stores",
        "evals/runs/_probe.py:2  configure_default_stores",
    ]

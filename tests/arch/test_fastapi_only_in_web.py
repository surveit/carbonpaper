"""Architecture: fastapi is only imported by app.web or the entrypoint.

The FastAPI() instance is constructed in app.main (app/main.py); every route
module lives under app/web/ (including app/web/routers/). No other subsystem —
app.core, app.services, app.compiler, app.runtime, app.evals, app.mcp,
app.agents — may import fastapi (or one of its submodules, e.g.
fastapi.responses): HTTP/routing concerns stay out of the domain and infra
layers.

This is a default-DENY allowlist, not a forbidden-module list: the only two
permitted locations are named explicitly (app/web/** and app/main.py), so a
brand-new module that imports fastapi fails automatically — there is nothing
to remember to add to a ban list. This replaces the former import-linter
`forbidden` contract ("fastapi stays in the web layer"), which was
default-ALLOW (a new module escaped it until someone added it to
`source_modules`) and carried a permanent `ignore_imports` exception for
app.compiler.router — that router has since moved into app.web (as
app/web/routers/editing.py), so the exception no longer exists.
"""
from __future__ import annotations

import ast
from pathlib import Path

from arch._helpers import find_imported_modules, parse_module
from arch.scope import find_source_files_under

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"
_ENTRYPOINT = "app/main.py"
_PERMITTED_DIR_PREFIX = "app/web/"


def is_permitted_fastapi_importer(relative_path: str) -> bool:
    """True if a repo-root-relative posix path is one of the two locations
    allowed to import fastapi: anywhere under app/web/, or the app.main
    entrypoint itself. Everything else is denied by default."""
    return relative_path == _ENTRYPOINT or relative_path.startswith(_PERMITTED_DIR_PREFIX)


def find_fastapi_imports(tree: ast.Module) -> list[str]:
    """Names of every fastapi import in `tree`: bare `fastapi` and any
    submodule (`fastapi.responses`, ...), via the module's own dotted name."""
    return sorted(
        name for name in find_imported_modules(tree)
        if name == "fastapi" or name.startswith("fastapi.")
    )


def find_disallowed_fastapi_importers(paths: list[Path], repo_root: Path) -> list[str]:
    """"<path>  imports <module>" for every file under `paths` that imports
    fastapi and is not one of the permitted locations (see
    `is_permitted_fastapi_importer`)."""
    offenders: list[str] = []
    for path in paths:
        relative = path.relative_to(repo_root).as_posix()
        if is_permitted_fastapi_importer(relative):
            continue
        for module in find_fastapi_imports(parse_module(path)):
            offenders.append(f"{relative}  imports {module}")
    return offenders


def test_fastapi_is_only_imported_by_app_web_or_the_entrypoint() -> None:
    offenders = find_disallowed_fastapi_importers(find_source_files_under(_APP_ROOT), _REPO_ROOT)
    assert not offenders, (
        "fastapi may only be imported under app/web/ or by the app.main "
        "entrypoint — HTTP/routing concerns stay out of the domain and infra "
        "layers:\n  " + "\n  ".join(offenders)
    )


# --- unit tests for the checker, on inline snippets (red + green) ---------


def test_is_permitted_fastapi_importer_accepts_the_entrypoint() -> None:
    assert is_permitted_fastapi_importer("app/main.py") is True


def test_is_permitted_fastapi_importer_accepts_a_web_router() -> None:
    assert is_permitted_fastapi_importer("app/web/routers/project.py") is True


def test_is_permitted_fastapi_importer_accepts_app_web_itself() -> None:
    assert is_permitted_fastapi_importer("app/web/chat_router.py") is True


def test_is_permitted_fastapi_importer_rejects_a_non_web_module() -> None:
    assert is_permitted_fastapi_importer("app/compiler/data_model.py") is False


def test_is_permitted_fastapi_importer_rejects_a_module_merely_prefixed_web() -> None:
    """A directory literally named "app/webhooks" must not slip in on a naive
    string prefix of "app/web" (without the trailing slash) — only the real
    app/web/ package is permitted."""
    assert is_permitted_fastapi_importer("app/webhooks/handler.py") is False


def test_find_fastapi_imports_flags_bare_import() -> None:
    tree = ast.parse("import fastapi\n")
    assert find_fastapi_imports(tree) == ["fastapi"]


def test_find_fastapi_imports_flags_top_level_from_import() -> None:
    tree = ast.parse("from fastapi import APIRouter\n")
    assert find_fastapi_imports(tree) == ["fastapi"]


def test_find_fastapi_imports_flags_submodule_from_import() -> None:
    tree = ast.parse("from fastapi.responses import RedirectResponse\n")
    assert find_fastapi_imports(tree) == ["fastapi.responses"]


def test_find_fastapi_imports_ignores_clean_snippet() -> None:
    tree = ast.parse("from app.services import project\n")
    assert find_fastapi_imports(tree) == []


def test_find_disallowed_fastapi_importers_flags_a_non_web_module(tmp_path: Path) -> None:
    target = tmp_path / "app" / "compiler"
    target.mkdir(parents=True)
    offender = target / "router.py"
    offender.write_text("from fastapi import APIRouter\n")
    assert find_disallowed_fastapi_importers([offender], tmp_path) == [
        "app/compiler/router.py  imports fastapi"
    ]


def test_find_disallowed_fastapi_importers_permits_a_web_module(tmp_path: Path) -> None:
    target = tmp_path / "app" / "web" / "routers"
    target.mkdir(parents=True)
    permitted = target / "project.py"
    permitted.write_text("from fastapi import APIRouter\n")
    assert find_disallowed_fastapi_importers([permitted], tmp_path) == []


def test_find_disallowed_fastapi_importers_permits_the_entrypoint(tmp_path: Path) -> None:
    target = tmp_path / "app"
    target.mkdir(parents=True)
    entrypoint = target / "main.py"
    entrypoint.write_text("from fastapi import FastAPI\n")
    assert find_disallowed_fastapi_importers([entrypoint], tmp_path) == []

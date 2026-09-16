from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from arch._helpers import find_imported_modules, find_relative_import_targets, parse_module
from arch.scope import find_source_files_under

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HARNESS_ROOT = _REPO_ROOT / "evals" / "harness"
_HARNESS_PACKAGE = "evals.harness"


def test_eval_harness_imports_only_the_standard_library_pydantic_and_itself() -> None:
    offenders = find_disallowed_harness_imports(find_source_files_under(_HARNESS_ROOT), _REPO_ROOT)
    assert not offenders, (
        "evals/harness/ is shared by every eval, so it may import only the standard library, "
        "pydantic and evals.harness itself — never app/, another package under evals/, or any "
        "other third-party library:\n  " + "\n  ".join(offenders)
    )


def find_disallowed_harness_imports(paths: list[Path], repo_root: Path) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        relative = path.relative_to(repo_root)
        tree = parse_module(path)
        imported = find_imported_modules(tree) | find_relative_import_targets(
            tree, relative.parent.parts
        )
        offenders += [
            f"{relative.as_posix()}: {module}"
            for module in sorted(imported)
            if not is_allowed_harness_import(module)
        ]
    return offenders


def is_allowed_harness_import(module: str) -> bool:
    top_level = module.split(".")[0]
    return (
        top_level in sys.stdlib_module_names
        or top_level == "pydantic"
        or module == _HARNESS_PACKAGE
        or module.startswith(f"{_HARNESS_PACKAGE}.")
    )


# --- unit tests for the checker, on inline snippets (red + green) ---------


@pytest.mark.parametrize(
    "module",
    ["__future__", "json", "collections.abc", "pydantic", "pydantic.fields", "evals.harness", "evals.harness.cases"],
)
def test_is_allowed_harness_import_accepts_the_standard_library_pydantic_and_the_harness(module: str) -> None:
    assert is_allowed_harness_import(module)


@pytest.mark.parametrize(
    "module",
    ["app", "app.models", "evals", "evals.runs.cli", "evals.harness_extra", "pandas", "pydantic_core"],
)
def test_is_allowed_harness_import_rejects_the_app_other_evals_and_other_libraries(module: str) -> None:
    assert not is_allowed_harness_import(module)


def test_find_relative_import_targets_resolves_each_level_against_the_package() -> None:
    tree = ast.parse("from . import cases\nfrom .scoring import score\nfrom ..runs.cli import main\n")
    assert find_relative_import_targets(tree, ("evals", "harness")) == {
        "evals.harness.cases",
        "evals.harness.scoring",
        "evals.runs.cli",
    }


def test_find_relative_import_targets_leaves_a_climb_past_the_top_package_unresolved() -> None:
    tree = ast.parse("from ... import app\n")
    assert find_relative_import_targets(tree, ("evals", "harness")) == {"...app"}


def test_find_relative_import_targets_ignores_absolute_imports() -> None:
    tree = ast.parse("import json\nfrom app import models\n")
    assert find_relative_import_targets(tree, ("evals", "harness")) == set()


def test_find_disallowed_harness_imports_names_every_offender_with_its_path(tmp_path: Path) -> None:
    harness = tmp_path / "evals" / "harness"
    harness.mkdir(parents=True)
    probe = harness / "probe.py"
    probe.write_text(
        "import json\n"
        "import pandas\n"
        "from pydantic import BaseModel\n"
        "from evals.harness.cases import Case\n"
        "from .scoring import score\n"
        "from ..runs import cli\n"
        "if TYPE_CHECKING:\n"
        "    from app.models import Stage\n",
        encoding="utf-8",
    )
    assert find_disallowed_harness_imports([probe], tmp_path) == [
        "evals/harness/probe.py: app.models",
        "evals/harness/probe.py: evals.runs",
        "evals/harness/probe.py: pandas",
    ]

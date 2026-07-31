from __future__ import annotations

from pathlib import Path

import pytest

from arch.import_allowlist import find_disallowed_imports


def test_allows_a_listed_submodule_in_both_import_forms(tmp_path: Path) -> None:
    ok = tmp_path / "ok.py"
    ok.write_text(
        "import app.services.project\n"
        "from app.services import project\n"
        "from app.services.project import export_project\n"
    , encoding="utf-8")
    assert find_disallowed_imports([ok], roots={"app"}, allow={"app.services.project"}) == []


def test_flags_a_sibling_of_a_listed_submodule(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("from app.services.loader import load\n", encoding="utf-8")
    offenders = find_disallowed_imports([bad], roots={"app"}, allow={"app.services.project"})
    assert len(offenders) == 1
    assert "app.services.loader" in offenders[0]


def test_flags_an_unlisted_module_without_naming_it(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("import sqlite3\nfrom app.core.persistence import read_doc\n", encoding="utf-8")
    offenders = find_disallowed_imports(
        [bad], roots={"app", "sqlite3"}, allow={"app.core.errors"}
    )
    assert len(offenders) == 2
    assert "sqlite3" in offenders[0]
    assert "app.core.persistence" in offenders[1]


def test_ignores_imports_outside_the_governed_roots(tmp_path: Path) -> None:
    ok = tmp_path / "ok.py"
    ok.write_text("import json\nimport pandas as pd\nfrom pathlib import Path\n", encoding="utf-8")
    assert find_disallowed_imports([ok], roots={"app"}, allow=set()) == []


def test_flags_a_relative_import(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("from . import loader\n", encoding="utf-8")
    offenders = find_disallowed_imports([bad], roots={"app"}, allow={"app.core.utils"})
    assert len(offenders) == 1
    assert "from . import loader" in offenders[0]


def test_reports_the_line_of_the_offending_import(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("import json\n\nfrom app.runtime.runner import prepare_run\n", encoding="utf-8")
    offenders = find_disallowed_imports([bad], roots={"app"}, allow=set())
    assert offenders[0].endswith(":3  from app.runtime.runner import prepare_run")


def test_raises_when_it_scans_nothing() -> None:
    with pytest.raises(ValueError, match="no files"):
        find_disallowed_imports([], roots={"app"}, allow=set())
